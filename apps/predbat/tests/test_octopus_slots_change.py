# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for octopus_slots_signature - the change-detection signature used by
fetch_sensor_data to decide whether a genuinely new/changed set of Octopus
intelligent dispatch slots warrants forcing a replan.

An in-progress dispatch has its start advanced to 'now' and its charge_in_kwh
scaled to the remaining time on every component refresh (octopus.py
async_get_intelligent_devices). That re-clocking must NOT be treated as a slot
change, otherwise a replan is forced on every cycle throughout an active
charging window. Genuine changes (new/removed slots, changed window end,
energy of a future slot, source/location) must still be detected.
"""

from datetime import datetime


def _slot(start, end, kwh, source="smart-charge", location="AT_HOME"):
    """Build an octopus dispatch slot dict"""
    return {"start": start, "end": end, "charge_in_kwh": kwh, "source": source, "location": location}


def test_octopus_slots_change(my_predbat):
    """
    Test octopus_slots_signature ignores in-progress re-clocking but detects genuine changes.

    Tests:
    - Test 1: In-progress slot re-clock (start + charge_in_kwh drift) → same signature
    - Test 2: A new future slot appears → different signature
    - Test 3: In-progress slot window end changes → different signature
    - Test 4: A future slot's energy changes → different signature
    - Test 5: A slot transitioning from future to active → different signature
    """
    print("**** Running octopus_slots_signature tests ****")
    failed = False

    old_now_utc = my_predbat.now_utc
    my_predbat.now_utc = datetime.strptime("2025-01-15T10:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")

    try:
        # Test 1: in-progress slot re-clocked (start advanced, energy scaled down) - NOT a real change
        print("*** Test 1: in-progress re-clock produces same signature ***")
        prev = [[_slot("2025-01-15T09:57:30+00:00", "2025-01-15T11:00:00+00:00", 7.2)]]
        curr = [[_slot("2025-01-15T09:59:40+00:00", "2025-01-15T11:00:00+00:00", 6.9)]]
        if my_predbat.octopus_slots_signature(prev) != my_predbat.octopus_slots_signature(curr):
            print("ERROR: Re-clocked in-progress slot reported as changed")
            failed = True
        else:
            print("Test 1 passed - re-clocking does not force a replan")

        # Test 2: a genuinely new future slot appears - IS a real change
        print("*** Test 2: new future slot produces different signature ***")
        curr_new = [[_slot("2025-01-15T09:59:40+00:00", "2025-01-15T11:00:00+00:00", 6.9), _slot("2025-01-15T13:00:00+00:00", "2025-01-15T14:00:00+00:00", 5.0)]]
        if my_predbat.octopus_slots_signature(prev) == my_predbat.octopus_slots_signature(curr_new):
            print("ERROR: New future slot not detected as a change")
            failed = True
        else:
            print("Test 2 passed - new slot forces a replan")

        # Test 3: the in-progress slot's window end changes (Octopus extended it) - IS a real change
        print("*** Test 3: in-progress slot end change produces different signature ***")
        curr_end = [[_slot("2025-01-15T09:59:40+00:00", "2025-01-15T11:30:00+00:00", 6.9)]]
        if my_predbat.octopus_slots_signature(prev) == my_predbat.octopus_slots_signature(curr_end):
            print("ERROR: In-progress slot end change not detected")
            failed = True
        else:
            print("Test 3 passed - end change forces a replan")

        # Test 4: a future slot's energy is revised - IS a real change
        print("*** Test 4: future slot energy change produces different signature ***")
        base_future = [[_slot("2025-01-15T13:00:00+00:00", "2025-01-15T14:00:00+00:00", 5.0)]]
        changed_future = [[_slot("2025-01-15T13:00:00+00:00", "2025-01-15T14:00:00+00:00", 6.0)]]
        if my_predbat.octopus_slots_signature(base_future) == my_predbat.octopus_slots_signature(changed_future):
            print("ERROR: Future slot energy change not detected")
            failed = True
        else:
            print("Test 4 passed - future slot energy change forces a replan")

        # Test 5: a slot transitioning from future to active - IS a real change (protection must engage)
        print("*** Test 5: future to active transition produces different signature ***")
        future = [[_slot("2025-01-15T10:30:00+00:00", "2025-01-15T11:00:00+00:00", 3.0)]]
        active = [[_slot("2025-01-15T10:00:00+00:00", "2025-01-15T11:00:00+00:00", 3.0)]]
        if my_predbat.octopus_slots_signature(future) == my_predbat.octopus_slots_signature(active):
            print("ERROR: Future to active transition not detected")
            failed = True
        else:
            print("Test 5 passed - future to active transition forces a replan")
    finally:
        my_predbat.now_utc = old_now_utc

    if not failed:
        print("**** All octopus_slots_signature tests PASSED ****")
    else:
        print("**** Some octopus_slots_signature tests FAILED ****")

    return failed
