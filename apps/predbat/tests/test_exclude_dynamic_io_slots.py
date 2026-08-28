# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for Octopus.exclude_dynamic_io_slots() (#4516) - undoes an IOG dispatch discount that
arrived via the rate feed itself (self.io_adjusted), independently of rate_add_io_slots()'s own
octopus_slots-driven overlay, for any dynamic minute rate_add_io_slots() didn't itself trust -
consulting self.trusted_dynamic_minutes (built by rate_add_io_slots()) rather than re-deriving
trust here, so the two functions can never disagree.
"""


def run_exclude_dynamic_io_slots_tests(my_predbat):
    """
    Test for exclude_dynamic_io_slots()
    """
    failed = False
    print("**** Running exclude_dynamic_io_slots tests ****")

    saved_io_adjusted = dict(my_predbat.io_adjusted)
    saved_trusted_dynamic_minutes = set(my_predbat.trusted_dynamic_minutes)
    saved_rate_max_base = my_predbat.rate_max_base
    saved_trust = my_predbat.trust_future_dynamic_iog_slots
    saved_minutes_now = my_predbat.minutes_now

    my_predbat.rate_max_base = 30.0
    # A trust level that actually engages the mechanism - it is deliberately a no-op at "planned"
    # (tested separately below) - and a "now" early enough that every minute under test is future.
    my_predbat.trust_future_dynamic_iog_slots = "none"
    my_predbat.minutes_now = 0

    try:
        print("Test 1: a dynamic io_adjusted minute not in trusted_dynamic_minutes is restored and cleared")
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.io_adjusted = {840: True}  # 14:00 - well outside 23:30-05:30
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 30.0:
            print("  ERROR: expected minute 840 restored to rate_max_base 30.0, got {}".format(result.get(840)))
            failed = True
        if 840 in my_predbat.io_adjusted:
            print("  ERROR: expected minute 840 cleared from io_adjusted, still present")
            failed = True

        print("Test 2: a fixed-window io_adjusted minute is left untouched even if untrusted")
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.io_adjusted = {120: True}  # 02:00 - inside 23:30-05:30
        rates = {120: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(120) != 4.0:
            print("  ERROR: expected minute 120 (fixed window) untouched at 4.0, got {}".format(result.get(120)))
            failed = True
        if 120 not in my_predbat.io_adjusted:
            print("  ERROR: expected minute 120 to remain marked io_adjusted, was cleared")
            failed = True

        print("Test 3: a dynamic io_adjusted minute that IS in trusted_dynamic_minutes is left untouched")
        my_predbat.trusted_dynamic_minutes = {840}
        my_predbat.io_adjusted = {840: True}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected trusted minute 840 untouched, got {}".format(result.get(840)))
            failed = True
        if 840 not in my_predbat.io_adjusted:
            print("  ERROR: expected trusted minute 840 to remain marked io_adjusted, was cleared")
            failed = True

        print("Test 4: a falsy io_adjusted entry outside the window is left untouched")
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.io_adjusted = {840: False}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected minute 840 (falsy io_adjusted) untouched, got {}".format(result.get(840)))
            failed = True

        print("Test 5: empty io_adjusted does not crash and returns rates unchanged")
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.io_adjusted = {}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected rates unchanged with empty io_adjusted, got {}".format(result.get(840)))
            failed = True

        print("Test 6: a mix of trusted, untrusted and fixed-window minutes are each handled independently")
        my_predbat.trusted_dynamic_minutes = {870}  # only this one is trusted
        my_predbat.io_adjusted = {840: True, 870: True, 120: True}
        rates = {840: 4.0, 870: 4.0, 120: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 30.0:
            print("  ERROR: expected untrusted dynamic minute 840 restored, got {}".format(result.get(840)))
            failed = True
        if result.get(870) != 4.0:
            print("  ERROR: expected trusted dynamic minute 870 untouched, got {}".format(result.get(870)))
            failed = True
        if result.get(120) != 4.0:
            print("  ERROR: expected the fixed-window minute untouched, got {}".format(result.get(120)))
            failed = True
        if 840 in my_predbat.io_adjusted:
            print("  ERROR: expected untrusted minute 840 cleared from io_adjusted")
            failed = True
        if 870 not in my_predbat.io_adjusted:
            print("  ERROR: expected trusted minute 870 to remain in io_adjusted")
            failed = True
        if 120 not in my_predbat.io_adjusted:
            print("  ERROR: expected the fixed-window minute to remain in io_adjusted")
            failed = True

        print("Test 7: an elapsed (past) untrusted dynamic minute is left untouched - it records what was actually charged")
        my_predbat.minutes_now = 900
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.io_adjusted = {840: True, 960: True}
        rates = {840: 4.0, 960: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected past minute 840 untouched at 4.0, got {}".format(result.get(840)))
            failed = True
        if 840 not in my_predbat.io_adjusted:
            print("  ERROR: expected past minute 840 to remain marked io_adjusted, was cleared")
            failed = True
        if result.get(960) != 30.0:
            print("  ERROR: expected future minute 960 restored to rate_max_base 30.0, got {}".format(result.get(960)))
            failed = True
        if 960 in my_predbat.io_adjusted:
            print("  ERROR: expected future minute 960 cleared from io_adjusted, still present")
            failed = True
        my_predbat.minutes_now = 0

        print("Test 8: at trust level 'planned' the whole mechanism is a no-op, even for an untrusted minute")
        my_predbat.trust_future_dynamic_iog_slots = "planned"
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.io_adjusted = {840: True}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected minute 840 untouched at 'planned', got {}".format(result.get(840)))
            failed = True
        if 840 not in my_predbat.io_adjusted:
            print("  ERROR: expected minute 840 to remain marked io_adjusted at 'planned', was cleared")
            failed = True
        my_predbat.trust_future_dynamic_iog_slots = "none"
    finally:
        my_predbat.io_adjusted = saved_io_adjusted
        my_predbat.trusted_dynamic_minutes = saved_trusted_dynamic_minutes
        my_predbat.rate_max_base = saved_rate_max_base
        my_predbat.trust_future_dynamic_iog_slots = saved_trust
        my_predbat.minutes_now = saved_minutes_now

    if failed:
        print("\n**** exclude_dynamic_io_slots tests: FAILED ****")
    else:
        print("\n**** exclude_dynamic_io_slots tests: PASSED ****")

    return failed
