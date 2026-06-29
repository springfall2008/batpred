# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for Output.load_today_comparison().

Covered scenarios
-----------------
1. None-guard regression: when filtered_today() cannot find a matching
   timestamp in load_adjusted_stamp or load_predict_stamp (because now_utc
   is not on a 5-minute boundary), it returns None.  Without the guard,
   dp2(None) raises:
       TypeError: type NoneType doesn't define __round__ method
   This test verifies the guard prevents the crash and the dashboard
   attributes are published as numeric values (0) rather than None.
"""

from datetime import datetime, timedelta

import pytz

UTC = pytz.UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _test_none_guard_no_crash(my_predbat, failed):
    """
    Verify load_today_comparison does not crash with TypeError when
    filtered_today returns None for load_so_far/load_today.

    Trigger: set now_utc to a non-5-minute-boundary timestamp.
    load_adjusted_stamp and load_predict_stamp only contain entries at
    exact 5-minute boundaries, so filtered_today(stamp=now_utc) returns None
    for load_so_far in both the load_energy_predicted and load_energy_adjusted
    dashboard items.
    """
    print("  test: None guard prevents crash when filtered_today returns None")

    # Save state that will be mutated
    saved_now_utc = my_predbat.now_utc
    saved_midnight_utc = my_predbat.midnight_utc
    saved_minutes_now = my_predbat.minutes_now

    # Use a fixed midnight so the test is deterministic
    midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    # Set now_utc to 1 minute past midnight - NOT a 5-minute boundary.
    # load_adjusted_stamp / load_predict_stamp only have keys at 0, 5, 10, ...
    # minutes past midnight, so filtered_today(stamp=now_utc) returns None
    # for load_so_far, exercising the None guard in the dashboard_item call.
    my_predbat.midnight_utc = midnight_utc
    my_predbat.now_utc = midnight_utc + timedelta(minutes=1)
    my_predbat.minutes_now = 0  # 5-min aligned start of day

    # Call with empty dicts - no load/import history; save=True triggers
    # the dashboard_item calls that formerly crashed on dp2(None).
    try:
        my_predbat.load_today_comparison({}, {}, {}, {}, minutes_now=0, save=True)
    except TypeError as e:
        print("  ERROR: load_today_comparison raised TypeError: {}".format(e))
        failed = True
        my_predbat.now_utc = saved_now_utc
        my_predbat.midnight_utc = saved_midnight_utc
        my_predbat.minutes_now = saved_minutes_now
        return failed

    # Verify that the attributes published are numeric (not None/crashing)
    for entity_suffix in [".load_energy_adjusted", ".load_energy_predicted"]:
        entity_id = my_predbat.prefix + entity_suffix
        attrs = my_predbat.dashboard_values.get(entity_id, {}).get("attributes", {})
        for attr_name in ["today_so_far", "today", "today_remaining"]:
            attr_val = attrs.get(attr_name, None)
            if not isinstance(attr_val, (int, float)):
                print("  ERROR: {}.{} = {} (expected numeric, got {})".format(entity_suffix, attr_name, attr_val, type(attr_val)))
                failed = True

    if not failed:
        print("  PASS: No TypeError raised and all load_energy attributes are numeric")

    # Restore state
    my_predbat.now_utc = saved_now_utc
    my_predbat.midnight_utc = saved_midnight_utc
    my_predbat.minutes_now = saved_minutes_now

    return failed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_load_today_comparison(my_predbat):
    """
    Unit tests for load_today_comparison() covering the None-guard fix
    for dp2() calls when filtered_today() returns None.
    """
    failed = False
    print("**** Running load_today_comparison tests ****")

    failed = _test_none_guard_no_crash(my_predbat, failed)

    return failed
