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
octopus_slots-driven overlay, for any minute outside the fixed 23:30-05:30 window when
trust_future_dynamic_iog_slots is off.
"""


def run_exclude_dynamic_io_slots_tests(my_predbat):
    """
    Test for exclude_dynamic_io_slots()
    """
    failed = False
    print("**** Running exclude_dynamic_io_slots tests ****")

    saved_trust_dynamic = my_predbat.trust_future_dynamic_iog_slots
    saved_io_adjusted = dict(my_predbat.io_adjusted)
    saved_minutes_now = my_predbat.minutes_now
    my_predbat.rate_max_base = 30.0
    my_predbat.minutes_now = 600  # 10:00, so current_block = 600

    try:
        print("Test 1: a dynamic (out-of-window) io_adjusted minute is restored to rate_max_base and cleared")
        my_predbat.trust_future_dynamic_iog_slots = False
        my_predbat.io_adjusted = {840: True}  # 14:00 - well outside 23:30-05:30
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 30.0:
            print("  ERROR: expected minute 840 restored to rate_max_base 30.0, got {}".format(result.get(840)))
            failed = True
        if 840 in my_predbat.io_adjusted:
            print("  ERROR: expected minute 840 cleared from io_adjusted, still present")
            failed = True

        print("Test 2: a fixed-window io_adjusted minute is left untouched")
        my_predbat.trust_future_dynamic_iog_slots = False
        my_predbat.io_adjusted = {120: True}  # 02:00 - inside 23:30-05:30
        rates = {120: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(120) != 4.0:
            print("  ERROR: expected minute 120 (fixed window) untouched at 4.0, got {}".format(result.get(120)))
            failed = True
        if 120 not in my_predbat.io_adjusted:
            print("  ERROR: expected minute 120 to remain marked io_adjusted, was cleared")
            failed = True

        print("Test 3: switch on leaves dynamic minutes untouched too")
        my_predbat.trust_future_dynamic_iog_slots = True
        my_predbat.io_adjusted = {840: True}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected minute 840 untouched with switch on, got {}".format(result.get(840)))
            failed = True
        if 840 not in my_predbat.io_adjusted:
            print("  ERROR: expected minute 840 to remain marked io_adjusted with switch on, was cleared")
            failed = True

        print("Test 4: a falsy io_adjusted entry outside the window is left untouched")
        my_predbat.trust_future_dynamic_iog_slots = False
        my_predbat.io_adjusted = {840: False}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected minute 840 (falsy io_adjusted) untouched, got {}".format(result.get(840)))
            failed = True

        print("Test 5: empty io_adjusted does not crash and returns rates unchanged")
        my_predbat.trust_future_dynamic_iog_slots = False
        my_predbat.io_adjusted = {}
        rates = {840: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 4.0:
            print("  ERROR: expected rates unchanged with empty io_adjusted, got {}".format(result.get(840)))
            failed = True

        print("Test 6: multiple dynamic minutes are all restored, a mixed-in fixed-window minute is not")
        my_predbat.trust_future_dynamic_iog_slots = False
        my_predbat.io_adjusted = {840: True, 870: True, 120: True}
        rates = {840: 4.0, 870: 4.0, 120: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(840) != 30.0 or result.get(870) != 30.0:
            print("  ERROR: expected both dynamic minutes restored, got {} and {}".format(result.get(840), result.get(870)))
            failed = True
        if result.get(120) != 4.0:
            print("  ERROR: expected the fixed-window minute untouched, got {}".format(result.get(120)))
            failed = True
        if 840 in my_predbat.io_adjusted or 870 in my_predbat.io_adjusted:
            print("  ERROR: expected both dynamic minutes cleared from io_adjusted")
            failed = True
        if 120 not in my_predbat.io_adjusted:
            print("  ERROR: expected the fixed-window minute to remain in io_adjusted")
            failed = True

        # Test 7 (#4516 follow-up): a dynamic io_adjusted minute already underway or in the past
        # (minutes_now=600, so current_block=600) is left untouched regardless of the switch -
        # today_cost()'s actual-spend figures need genuine historical rates, not ones retroactively
        # excluded once we know the dispatch already happened.
        print("Test 7: a past/current dynamic io_adjusted minute is left untouched regardless of the switch")
        my_predbat.trust_future_dynamic_iog_slots = False
        my_predbat.io_adjusted = {570: True}  # 09:30 - before minutes_now (10:00), outside the fixed window
        rates = {570: 4.0}
        result = my_predbat.exclude_dynamic_io_slots(rates)
        if result.get(570) != 4.0:
            print("  ERROR: expected past minute 570 untouched at 4.0, got {}".format(result.get(570)))
            failed = True
        if 570 not in my_predbat.io_adjusted:
            print("  ERROR: expected past minute 570 to remain marked io_adjusted, was cleared")
            failed = True
    finally:
        my_predbat.trust_future_dynamic_iog_slots = saved_trust_dynamic
        my_predbat.io_adjusted = saved_io_adjusted
        my_predbat.minutes_now = saved_minutes_now

    if failed:
        print("\n**** exclude_dynamic_io_slots tests: FAILED ****")
    else:
        print("\n**** exclude_dynamic_io_slots tests: PASSED ****")

    return failed
