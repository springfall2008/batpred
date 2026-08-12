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
2. Import-exceeds-load regression (batpred#4154, #2537): when grid import for
   a bucket is >= that bucket's raw house load (e.g. overnight battery
   charging pulling more than the house is drawing), the house's own genuine
   load must still be counted in the actual/predicted totals - only the
   import beyond the load was going to the battery. Previously the whole
   bucket's load was zeroed out of both totals whenever this happened.
"""

import re
from datetime import datetime, timedelta

import pytz

from utils import MinuteArray

UTC = pytz.UTC


def build_cumulative(per_minute, size):
    """Build a backwards cumulative MinuteArray with a constant per-minute increment.

    get_from_incrementing(data, i) == per_minute for every in-range i, so any 5-minute
    (or other step) window sums to per_minute * step regardless of exactly which indices
    a caller's own offset convention picks.
    """
    data = {}
    data[size - 1] = 0.0
    for i in range(size - 2, -1, -1):
        data[i] = data[i + 1] + per_minute
    return MinuteArray(data, size)


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


def _test_import_exceeding_load_still_counted(my_predbat, failed):
    """
    Verify load genuinely consumed during a heavy-import (e.g. overnight charging) bucket
    is still counted in the "so far" actual/predicted totals, not zeroed just because grid
    import for that bucket was >= the raw house load.

    Asserted via the function's own diagnostic log line rather than the published dashboard
    states, since those blend "so far" with a predicted-for-the-rest-of-today tail and are not
    a clean, minimal readout of the specific total this fix changes.
    """
    print("  test: import >= raw load no longer zeros genuine consumption")

    # Save state that will be mutated
    saved = {
        "car_charging_hold": my_predbat.car_charging_hold,
        "car_charging_energy": my_predbat.car_charging_energy,
        "iboost_energy_subtract": my_predbat.iboost_energy_subtract,
        "iboost_energy_today": my_predbat.iboost_energy_today,
        "base_load": my_predbat.base_load,
        "load_forecast_only": my_predbat.load_forecast_only,
        "days_previous": my_predbat.days_previous,
        "days_previous_weight": my_predbat.days_previous_weight,
        "load_minutes_age": my_predbat.load_minutes_age,
        "now_utc": my_predbat.now_utc,
        "midnight_utc": my_predbat.midnight_utc,
        "minutes_now": my_predbat.minutes_now,
        "log": my_predbat.log,
    }

    captured = []
    my_predbat.log = lambda msg, quiet=True: captured.append(msg)

    try:
        my_predbat.car_charging_hold = False
        my_predbat.car_charging_energy = None
        my_predbat.iboost_energy_subtract = False
        my_predbat.iboost_energy_today = None
        my_predbat.base_load = 0.0
        my_predbat.load_forecast_only = False
        my_predbat.days_previous = [1]
        my_predbat.days_previous_weight = [1.0]
        my_predbat.load_minutes_age = 1

        midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        minutes_now = 17  # not on a step=5 boundary, so "so far" cleanly covers 4 whole buckets
        my_predbat.midnight_utc = midnight_utc
        my_predbat.now_utc = midnight_utc + timedelta(minutes=minutes_now)
        my_predbat.minutes_now = minutes_now

        # Import: 0.1 kWh/min -> 0.5 kWh per 5-minute bucket
        import_minutes = build_cumulative(0.1, 3000)
        # House load: 0.05 kWh/min -> 0.25 kWh per 5-minute bucket, well below import,
        # so every "today so far" bucket trips the import >= raw-load condition.
        load_minutes = build_cumulative(0.05, 3000)
        load_forecast = {}

        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)

        expected_load = 0.25 * 4  # 4 whole buckets (minute 0,5,10,15) fall inside "so far"
        expected_ignored = (0.5 - 0.25) * 4  # excess import only, not the whole bucket

        actual_line = next((m for m in captured if m.startswith("Today's actual load so far")), None)
        predicted_line = next((m for m in captured if m.startswith("Today's predicted so far")), None)

        if actual_line is None or predicted_line is None:
            print("  ERROR: expected log lines not found - captured: {}".format(captured))
            failed = True
        else:
            actual_load = float(re.search(r"so far ([\d.]+)kWh", actual_line).group(1))
            actual_ignored = float(re.search(r"([\d.]+)kWh import ignored", actual_line).group(1))
            predicted_load = float(re.search(r"so far ([\d.]+)kWh", predicted_line).group(1))
            predicted_ignored = float(re.search(r"([\d.]+)kWh import ignored", predicted_line).group(1))

            if abs(actual_load - expected_load) > 0.01:
                print("  ERROR: actual load so far = {} (expected ~{})".format(actual_load, expected_load))
                failed = True
            if abs(actual_ignored - expected_ignored) > 0.01:
                print("  ERROR: actual import ignored = {} (expected ~{})".format(actual_ignored, expected_ignored))
                failed = True
            if abs(predicted_load - expected_load) > 0.01:
                print("  ERROR: predicted load so far = {} (expected ~{})".format(predicted_load, expected_load))
                failed = True
            if abs(predicted_ignored - expected_ignored) > 0.01:
                print("  ERROR: predicted import ignored = {} (expected ~{})".format(predicted_ignored, expected_ignored))
                failed = True

        if not failed:
            print("  PASS: genuine load during heavy-import buckets is counted, not zeroed ({:.2f}kWh, {:.2f}kWh excess ignored)".format(expected_load, expected_ignored))
    finally:
        my_predbat.car_charging_hold = saved["car_charging_hold"]
        my_predbat.car_charging_energy = saved["car_charging_energy"]
        my_predbat.iboost_energy_subtract = saved["iboost_energy_subtract"]
        my_predbat.iboost_energy_today = saved["iboost_energy_today"]
        my_predbat.base_load = saved["base_load"]
        my_predbat.load_forecast_only = saved["load_forecast_only"]
        my_predbat.days_previous = saved["days_previous"]
        my_predbat.days_previous_weight = saved["days_previous_weight"]
        my_predbat.load_minutes_age = saved["load_minutes_age"]
        my_predbat.now_utc = saved["now_utc"]
        my_predbat.midnight_utc = saved["midnight_utc"]
        my_predbat.minutes_now = saved["minutes_now"]
        my_predbat.log = saved["log"]

    return failed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_load_today_comparison(my_predbat):
    """
    Unit tests for load_today_comparison() covering the None-guard fix
    for dp2() calls when filtered_today() returns None, and the
    import-exceeds-load regression (batpred#4154, #2537).
    """
    failed = False
    print("**** Running load_today_comparison tests ****")

    failed = _test_none_guard_no_crash(my_predbat, failed)
    failed = _test_import_exceeding_load_still_counted(my_predbat, failed) or failed

    return failed
