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

    # Test 11: A stored slot must survive calculate_yesterday()'s temporary clock shift (#4900)
    # It sets minutes_now to 0 and winds midnight_utc back a day for the duration of the
    # savings calculation, and a web request decoding the selection can land inside that window.
    print("Test 11: Manual times survive the yesterday-savings clock shift")

    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=120)
    my_predbat.minutes_now = 120

    my_predbat.manual_select("manual_export", "off")
    my_predbat.manual_select("manual_export", "Fri 05:30")
    stored_before = my_predbat.config_index["manual_export"].get("value")

    save_minutes_now = my_predbat.minutes_now
    save_midnight_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0
    my_predbat.midnight_utc = my_predbat.midnight_utc - timedelta(days=1)
    my_predbat.manual_times("manual_export", update=False)
    my_predbat.manual_times("manual_export")
    my_predbat.minutes_now = save_minutes_now
    my_predbat.midnight_utc = save_midnight_utc

    stored_after = my_predbat.config_index["manual_export"].get("value")
    manual_export_times = my_predbat.manual_times("manual_export")

    if stored_after != stored_before:
        print("ERROR: T11 Stored selection moved from {} to {} across the clock shift".format(stored_before, stored_after))
        failed = True
    elif 330 not in manual_export_times:
        print("ERROR: T11 Expected minute 330 (05:30 today) but got {}".format(manual_export_times))
        failed = True
    else:
        print("PASS: T11 Manual export slot still resolves to {} after the clock shift".format(manual_export_times))

    # Test 12: The same for manual rates, and a read-only decode must not touch the stored item
    print("Test 12: Manual rates survive the shift and update=False does not write back")

    my_predbat.manual_select("manual_soc", "off")
    my_predbat.manual_select("manual_soc", "Fri 05:30=100.0")
    stored_before = my_predbat.config_index["manual_soc"].get("value")
    options_before = list(my_predbat.config_index["manual_soc"].get("options", []))

    save_minutes_now = my_predbat.minutes_now
    save_midnight_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0
    my_predbat.midnight_utc = my_predbat.midnight_utc - timedelta(days=1)
    my_predbat.manual_rates("manual_soc", update=False)
    stored_readonly = my_predbat.config_index["manual_soc"].get("value")
    options_readonly = list(my_predbat.config_index["manual_soc"].get("options", []))
    my_predbat.manual_rates("manual_soc")
    my_predbat.minutes_now = save_minutes_now
    my_predbat.midnight_utc = save_midnight_utc

    stored_after = my_predbat.config_index["manual_soc"].get("value")
    manual_soc_keep = my_predbat.manual_rates("manual_soc")

    if stored_readonly != stored_before or options_readonly != options_before:
        print("ERROR: T12 update=False rewrote the stored item {} / options changed {}".format(stored_readonly, options_readonly != options_before))
        failed = True
    elif stored_after != stored_before:
        print("ERROR: T12 Stored selection moved from {} to {} across the clock shift".format(stored_before, stored_after))
        failed = True
    elif manual_soc_keep.get(330, None) != 100.0:
        print("ERROR: T12 Expected SoC override 100.0 at minute 330 but got {}".format(manual_soc_keep.get(330, None)))
        failed = True
    else:
        print("PASS: T12 Manual SoC override survives the clock shift and read-only decode")

    # Test 13: A click landing inside the shift must not silently drop a slot more than a day
    # out - the 48 hour horizon is checked against the decoded minutes, so a clock a day behind
    # pushed anything beyond 24 hours past the limit (#4900)
    print("Test 13: A selection made during the clock shift keeps far-future slots")

    my_predbat.manual_select("manual_export", "off")
    my_predbat.manual_select("manual_export", "Fri 05:30")
    my_predbat.manual_select("manual_export", "Sat 18:00")

    save_minutes_now = my_predbat.minutes_now
    save_midnight_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0
    my_predbat.midnight_utc = my_predbat.midnight_utc - timedelta(days=1)
    my_predbat.manual_select("manual_export", "Fri 06:00")
    my_predbat.minutes_now = save_minutes_now
    my_predbat.midnight_utc = save_midnight_utc

    manual_export_times = my_predbat.manual_times("manual_export")
    expected_minutes = {330, 360, 2520}

    if not expected_minutes.issubset(set(manual_export_times)):
        print("ERROR: T13 Expected minutes {} but got {}".format(expected_minutes, manual_export_times))
        failed = True
    else:
        print("PASS: T13 Selecting during the clock shift kept every slot: {}".format(manual_export_times))

    # Test 14: A rate override several days out survives
    # Rate overrides carry a 7 day horizon, not the 48 hours a manual charge/export slot gets:
    # they are future tariff data that only has to be remembered until the plan reaches them,
    # which is how a supplier's free-electricity hour announced days ahead can be entered.
    print("Test 14: Rate override beyond 48 hours is retained")
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=0)  # Start at midnight
    my_predbat.minutes_now = 0
    my_predbat.manual_select("manual_import_rates", "off")

    far_day = (my_predbat.now_utc + timedelta(days=4)).strftime("%a")
    far_minute = 4 * 24 * 60 + 12 * 60
    my_predbat.manual_select("manual_import_rates", "{} 12:00=0.0".format(far_day))
    manual_import_far = my_predbat.manual_rates("manual_import_rates", default_rate=10.0)

    if far_minute not in manual_import_far:
        print("ERROR: T14 Expected minute {} ({} 12:00) to survive but got {}".format(far_minute, far_day, sorted(manual_import_far)))
        failed = True
    elif manual_import_far[far_minute] != 0.0:
        print("ERROR: T14 Expected rate 0.0 at minute {} but got {}".format(far_minute, manual_import_far[far_minute]))
        failed = True
    else:
        print("PASS: T14 Rate override 4 days ahead retained at minute {} rate {}".format(far_minute, manual_import_far[far_minute]))

    # Test 15: the same distance out is still rejected for a manual TIME slot, which stays bounded
    # by the plan horizon - Predbat can only act on a slot the plan actually reaches.
    print("Test 15: Manual time beyond 48 hours is still dropped")
    my_predbat.manual_select("manual_demand", "off")
    my_predbat.manual_select("manual_demand", "{} 12:00".format(far_day))
    manual_demand_far = my_predbat.manual_times("manual_demand")

    if far_minute in manual_demand_far:
        print("ERROR: T15 Expected minute {} to be dropped from manual_demand but got {}".format(far_minute, manual_demand_far))
        failed = True
    else:
        print("PASS: T15 Manual demand 4 days ahead correctly dropped: {}".format(manual_demand_far))

    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)

    # Clean up
    my_predbat.manual_select("manual_demand", "off")
    my_predbat.manual_select("manual_import_rates", "off")
    my_predbat.manual_select("manual_export", "off")
    my_predbat.manual_select("manual_soc", "off")
    return failed
