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


def test_add_now_to_octopus_slot(my_predbat):
    """
    Test the add_now_to_octopus_slot function
    
    This function adds a 30-minute slot to octopus_slots when a car is currently charging.
    It's used as a workaround for Ohme integration to ensure current charging is treated
    as an intelligent slot.
    
    Tests various scenarios:
    - Single car charging now
    - Multiple cars charging
    - No cars charging
    - Different times within 30-minute slots
    - Slot alignment to 30-minute boundaries
    """
    print("**** Running add_now_to_octopus_slot tests ****")
    failed = False
    
    # Setup test environment
    old_num_cars = my_predbat.num_cars
    old_car_charging_now = my_predbat.car_charging_now
    old_minutes_now = my_predbat.minutes_now
    old_midnight_utc = my_predbat.midnight_utc
    
    # Test 1: Single car charging at 10:15
    print("*** Test 1: Single car charging at 10:15 (rounds to 10:00-10:30 slot)")
    
    my_predbat.num_cars = 1
    my_predbat.car_charging_now = [True]
    my_predbat.minutes_now = 10 * 60 + 15  # 10:15
    my_predbat.midnight_utc = datetime.strptime("2025-01-15T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    # Should have one slot added
    if len(result) != 1:
        print("ERROR: Expected 1 slot, got {}".format(len(result)))
        failed = True
    else:
        slot = result[0]
        # Slot should be 10:00-10:30 (rounded down to 30-min boundary)
        expected_start = my_predbat.midnight_utc + timedelta(minutes=10 * 60)
        expected_end = my_predbat.midnight_utc + timedelta(minutes=10 * 60 + 30)
        
        if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected start {}, got {}".format(expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["start"]))
            failed = True
        
        if slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected end {}, got {}".format(expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["end"]))
            failed = True
        
        if not failed:
            print("Test 1 passed - single car slot added correctly")
    
    # Test 2: Car not charging - no slot should be added
    print("*** Test 2: Car not charging - no slot added")
    
    my_predbat.car_charging_now = [False]
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 0:
        print("ERROR: Expected 0 slots when car not charging, got {}".format(len(result)))
        failed = True
    else:
        print("Test 2 passed - no slot added when car not charging")
    
    # Test 3: Multiple cars, some charging
    print("*** Test 3: Multiple cars, only car 0 and car 2 charging")
    
    my_predbat.num_cars = 3
    my_predbat.car_charging_now = [True, False, True]
    my_predbat.minutes_now = 14 * 60 + 45  # 14:45
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    # Should have two slots (one for each charging car)
    if len(result) != 2:
        print("ERROR: Expected 2 slots for 2 charging cars, got {}".format(len(result)))
        failed = True
    else:
        # Both slots should be the same (14:30-15:00)
        expected_start = my_predbat.midnight_utc + timedelta(minutes=14 * 60 + 30)
        expected_end = my_predbat.midnight_utc + timedelta(minutes=15 * 60)
        
        for i, slot in enumerate(result):
            if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"):
                print("ERROR: Slot {} expected start {}, got {}".format(i, expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["start"]))
                failed = True
            
            if slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
                print("ERROR: Slot {} expected end {}, got {}".format(i, expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["end"]))
                failed = True
        
        if not failed:
            print("Test 3 passed - multiple charging cars add multiple slots")
    
    # Test 4: Charging at exact 30-minute boundary
    print("*** Test 4: Charging at exact 30-minute boundary (14:30)")
    
    my_predbat.num_cars = 1
    my_predbat.car_charging_now = [True]
    my_predbat.minutes_now = 14 * 60 + 30  # 14:30 exactly
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 1:
        print("ERROR: Expected 1 slot, got {}".format(len(result)))
        failed = True
    else:
        slot = result[0]
        # Should be 14:30-15:00
        expected_start = my_predbat.midnight_utc + timedelta(minutes=14 * 60 + 30)
        expected_end = my_predbat.midnight_utc + timedelta(minutes=15 * 60)
        
        if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected start {}, got {}".format(expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["start"]))
            failed = True
        
        if slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected end {}, got {}".format(expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["end"]))
            failed = True
        
        if not failed:
            print("Test 4 passed - exact boundary handled correctly")
    
    # Test 5: Charging at end of 30-minute slot
    print("*** Test 5: Charging at 10:29 (end of slot)")
    
    my_predbat.minutes_now = 10 * 60 + 29  # 10:29
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 1:
        print("ERROR: Expected 1 slot, got {}".format(len(result)))
        failed = True
    else:
        slot = result[0]
        # Should still be 10:00-10:30
        expected_start = my_predbat.midnight_utc + timedelta(minutes=10 * 60)
        expected_end = my_predbat.midnight_utc + timedelta(minutes=10 * 60 + 30)
        
        if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected start {}, got {}".format(expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["start"]))
            failed = True
        
        if slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected end {}, got {}".format(expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["end"]))
            failed = True
        
        if not failed:
            print("Test 5 passed - end of slot handled correctly")
    
    # Test 6: Charging just after midnight
    print("*** Test 6: Charging at 00:15 (just after midnight)")
    
    my_predbat.minutes_now = 15  # 00:15
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 1:
        print("ERROR: Expected 1 slot, got {}".format(len(result)))
        failed = True
    else:
        slot = result[0]
        # Should be 00:00-00:30
        expected_start = my_predbat.midnight_utc
        expected_end = my_predbat.midnight_utc + timedelta(minutes=30)
        
        if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected start {}, got {}".format(expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["start"]))
            failed = True
        
        if slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected end {}, got {}".format(expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["end"]))
            failed = True
        
        if not failed:
            print("Test 6 passed - just after midnight handled correctly")
    
    # Test 7: Charging late at night
    print("*** Test 7: Charging at 23:45 (late at night)")
    
    my_predbat.minutes_now = 23 * 60 + 45  # 23:45
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 1:
        print("ERROR: Expected 1 slot, got {}".format(len(result)))
        failed = True
    else:
        slot = result[0]
        # Should be 23:30-00:00 (end at midnight of next day)
        expected_start = my_predbat.midnight_utc + timedelta(minutes=23 * 60 + 30)
        expected_end = my_predbat.midnight_utc + timedelta(minutes=24 * 60)
        
        if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected start {}, got {}".format(expected_start.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["start"]))
            failed = True
        
        if slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
            print("ERROR: Expected end {}, got {}".format(expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"), slot["end"]))
            failed = True
        
        if not failed:
            print("Test 7 passed - late night slot handled correctly")
    
    # Test 8: Pre-existing slots should be preserved
    print("*** Test 8: Pre-existing slots preserved when adding new slot")
    
    my_predbat.minutes_now = 10 * 60 + 15  # 10:15
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    
    # Start with an existing slot
    existing_slot = {
        "start": "2025-01-15T08:00:00+00:00",
        "end": "2025-01-15T09:00:00+00:00"
    }
    octopus_slots = [existing_slot]
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 2:
        print("ERROR: Expected 2 slots (1 existing + 1 new), got {}".format(len(result)))
        failed = True
    else:
        # Check existing slot is still there
        if result[0] != existing_slot:
            print("ERROR: Existing slot was modified")
            failed = True
        else:
            print("Test 8 passed - existing slots preserved")
    
    # Test 9: Zero cars configured
    print("*** Test 9: Zero cars configured")
    
    my_predbat.num_cars = 0
    my_predbat.car_charging_now = []
    
    octopus_slots = []
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 0:
        print("ERROR: Expected 0 slots with no cars, got {}".format(len(result)))
        failed = True
    else:
        print("Test 9 passed - zero cars handled correctly")
    
    # Test 10: All cars not charging
    print("*** Test 10: Multiple cars but none charging")
    
    my_predbat.num_cars = 3
    my_predbat.car_charging_now = [False, False, False]
    
    octopus_slots = []
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 0:
        print("ERROR: Expected 0 slots when no cars charging, got {}".format(len(result)))
        failed = True
    else:
        print("Test 10 passed - no slots when no cars charging")
    
    # Test 11: All cars charging
    print("*** Test 11: All cars charging")
    
    my_predbat.num_cars = 2
    my_predbat.car_charging_now = [True, True]
    my_predbat.minutes_now = 12 * 60  # 12:00 exactly
    
    now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    octopus_slots = []
    
    result = my_predbat.add_now_to_octopus_slot(octopus_slots, now_utc)
    
    if len(result) != 2:
        print("ERROR: Expected 2 slots for 2 charging cars, got {}".format(len(result)))
        failed = True
    else:
        # Both should be the same slot (12:00-12:30)
        expected_start = my_predbat.midnight_utc + timedelta(minutes=12 * 60)
        expected_end = my_predbat.midnight_utc + timedelta(minutes=12 * 60 + 30)
        
        for i, slot in enumerate(result):
            if slot["start"] != expected_start.strftime("%Y-%m-%dT%H:%M:%S%z") or slot["end"] != expected_end.strftime("%Y-%m-%dT%H:%M:%S%z"):
                print("ERROR: Slot {} has incorrect times".format(i))
                failed = True
        
        if not failed:
            print("Test 11 passed - all cars charging adds correct slots")
    
    # Restore original values
    my_predbat.num_cars = old_num_cars
    my_predbat.car_charging_now = old_car_charging_now
    my_predbat.minutes_now = old_minutes_now
    my_predbat.midnight_utc = old_midnight_utc
    
    if not failed:
        print("**** All add_now_to_octopus_slot tests PASSED ****")
    else:
        print("**** Some add_now_to_octopus_slot tests FAILED ****")
    
    return failed
