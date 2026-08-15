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


def _test_load_scaling_applied_to_predicted(my_predbat, failed):
    """
    Regression test for issue #4496: the predicted load curve (which drives the
    today_remaining attribute on load_energy_predicted/load_energy_adjusted) must apply
    self.load_scaling, matching step_data_history() (fetch.py) - which builds the plan's own
    load_minutes_step as (value + load_extra) * scale_fixed, where scale_fixed includes
    load_scaling. Previously load_scaling was never applied in load_today_comparison, so with
    load_scaling != 1.0 the today_remaining attribute diverged from the plan's own remaining
    load total by exactly that factor. Confirmed against a real user report: their
    load_scaling of 1.05 produced a sensor value ~5.7% below the plan's own total, matching
    this factor almost exactly.
    """
    print("  test: load_scaling is applied to the predicted (today_remaining) load curve")

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
        "load_scaling": my_predbat.load_scaling,
        "now_utc": my_predbat.now_utc,
        "midnight_utc": my_predbat.midnight_utc,
        "minutes_now": my_predbat.minutes_now,
    }

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
        minutes_now = 780  # 13:00, well clear of the day boundary
        my_predbat.midnight_utc = midnight_utc
        my_predbat.now_utc = midnight_utc + timedelta(minutes=minutes_now)
        my_predbat.minutes_now = minutes_now

        load_minutes = build_cumulative(0.02, 3000)  # 0.02 kWh/min -> 0.1 kWh per 5-min bucket
        load_forecast = {}
        import_minutes = build_cumulative(0.0, 3000)

        my_predbat.load_scaling = 1.0
        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)
        baseline_predicted = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["attributes"]["today_remaining"]
        baseline_adjusted = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_adjusted"]["attributes"]["today_remaining"]

        my_predbat.load_scaling = 1.5
        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)
        scaled_predicted = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["attributes"]["today_remaining"]
        scaled_adjusted = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_adjusted"]["attributes"]["today_remaining"]

        if baseline_predicted <= 0 or baseline_adjusted <= 0:
            print("  ERROR: baseline today_remaining should be positive for this to be a meaningful test, got predicted={} adjusted={}".format(baseline_predicted, baseline_adjusted))
            failed = True
        else:
            # Each sensor is checked against its own baseline - load_energy_adjusted applies its
            # own in-day-adjustment factor on top, so its baseline can legitimately differ from
            # load_energy_predicted's, but both must independently scale by load_scaling.
            expected_predicted = round(baseline_predicted * 1.5, 2)
            expected_adjusted = round(baseline_adjusted * 1.5, 2)
            if abs(scaled_predicted - expected_predicted) > 0.02:
                print("  ERROR: load_energy_predicted today_remaining with load_scaling=1.5 should be ~{} (1.5x baseline {}), got {}".format(expected_predicted, baseline_predicted, scaled_predicted))
                failed = True
            if abs(scaled_adjusted - expected_adjusted) > 0.02:
                print("  ERROR: load_energy_adjusted today_remaining with load_scaling=1.5 should be ~{} (1.5x baseline {}), got {}".format(expected_adjusted, baseline_adjusted, scaled_adjusted))
                failed = True
            if not failed:
                print("  PASS: today_remaining scales with load_scaling on both sensors (predicted {} -> {}, adjusted {} -> {} at 1.5x)".format(baseline_predicted, scaled_predicted, baseline_adjusted, scaled_adjusted))
    finally:
        for key, value in saved.items():
            setattr(my_predbat, key, value)

    return failed


def _test_load_scaling_dynamic_and_manual_adjust_applied_to_predicted(my_predbat, failed):
    """
    Follow-up regression test for issue #4496: the fix in _test_load_scaling_applied_to_predicted
    only applied the flat self.load_scaling, but step_data_history() (fetch.py) also applies
    self.load_scaling_dynamic (per-minute - saving-session/free-electricity-event scaling, and
    any per-window override from rates_import_override/the manual API) and self.manual_load_adjust
    (additive, per-minute). Confirmed against a real follow-up user report where a 1.5x
    load_scaling_dynamic override for a 2-hour "power up" event wasn't reflected in
    today_remaining at all even after the first fix landed.
    """
    print("  test: load_scaling_dynamic and manual_load_adjust are applied to the predicted (today_remaining) load curve")

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
        "load_scaling": my_predbat.load_scaling,
        "load_scaling_dynamic": my_predbat.load_scaling_dynamic,
        "manual_load_adjust": my_predbat.manual_load_adjust,
        "now_utc": my_predbat.now_utc,
        "midnight_utc": my_predbat.midnight_utc,
        "minutes_now": my_predbat.minutes_now,
    }

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
        my_predbat.load_scaling = 1.0

        midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        minutes_now = 780  # 13:00, well clear of the day boundary
        my_predbat.midnight_utc = midnight_utc
        my_predbat.now_utc = midnight_utc + timedelta(minutes=minutes_now)
        my_predbat.minutes_now = minutes_now

        load_minutes = build_cumulative(0.02, 3000)  # 0.02 kWh/min -> 0.1 kWh per 5-min bucket
        load_forecast = {}
        import_minutes = build_cumulative(0.0, 3000)

        # Baseline: no dynamic scaling, no manual adjustment
        my_predbat.load_scaling_dynamic = {}
        my_predbat.manual_load_adjust = {}
        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)
        baseline_remaining = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["attributes"]["today_remaining"]

        # load_scaling_dynamic applied uniformly across every future minute at 2.0x - the whole
        # remaining-today total should double, exactly like the flat load_scaling test does
        my_predbat.load_scaling_dynamic = {minute: 2.0 for minute in range(minutes_now, 24 * 60, 5)}
        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)
        dynamic_scaled_remaining = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["attributes"]["today_remaining"]

        expected_dynamic = round(baseline_remaining * 2.0, 2)
        if abs(dynamic_scaled_remaining - expected_dynamic) > 0.02:
            print("  ERROR: today_remaining with load_scaling_dynamic=2.0 across the day should be ~{} (2x baseline {}), got {}".format(expected_dynamic, baseline_remaining, dynamic_scaled_remaining))
            failed = True
        else:
            print("  PASS: today_remaining scales with load_scaling_dynamic ({} -> {} at 2x)".format(baseline_remaining, dynamic_scaled_remaining))

        # manual_load_adjust applied additively at a single future minute
        my_predbat.load_scaling_dynamic = {}
        manual_adjust_minute = minutes_now + 60
        manual_adjust_kwh = 6.0
        my_predbat.manual_load_adjust = {manual_adjust_minute: manual_adjust_kwh}
        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)
        manual_adjusted_remaining = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["attributes"]["today_remaining"]

        expected_delta = manual_adjust_kwh * 5 / float(my_predbat.plan_interval_minutes)
        expected_manual = round(baseline_remaining + expected_delta, 2)
        if abs(manual_adjusted_remaining - expected_manual) > 0.02:
            print("  ERROR: today_remaining with a manual_load_adjust of {}kWh at minute {} should be ~{} (baseline {} + {:.2f}), got {}".format(manual_adjust_kwh, manual_adjust_minute, expected_manual, baseline_remaining, expected_delta, manual_adjusted_remaining))
            failed = True
        else:
            print("  PASS: today_remaining reflects manual_load_adjust ({} -> {}, +{:.2f}kWh)".format(baseline_remaining, manual_adjusted_remaining, expected_delta))
    finally:
        for key, value in saved.items():
            setattr(my_predbat, key, value)

    return failed


def _test_predicted_today_stable_across_the_day(my_predbat, failed):
    """
    Follow-up regression test for issue #4496: load_energy_predicted's state/"today" total
    (the whole midnight-to-midnight forecast, as opposed to "today_remaining") must not drift
    as minutes_now advances through the day when load_scaling != 1.0. The two fixes above
    (#4506, #4511) applied load_scaling/load_scaling_dynamic/manual_load_adjust to future
    minutes only, correctly fixing today_remaining, but load_total_pred - the same accumulator
    the whole-day "today" total was read from - summed those scaled future buckets alongside
    unscaled past buckets. As minutes_now advanced, buckets kept flipping from the scaled group
    to the unscaled one, so the published "today" total silently shrank (with load_scaling > 1)
    or grew (with load_scaling < 1) even though nothing about the underlying forecast changed -
    exactly the "prediction drops from 11.5kWh to 11.3kWh by 08:00" regression a user reported
    after taking the #4511 fix. This asserts the whole-day total is identical whether computed
    at the start of the day or partway through it, given the same underlying load model.
    """
    print("  test: load_energy_predicted's whole-day 'today' total is stable as minutes_now advances")

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
        "load_scaling": my_predbat.load_scaling,
        "load_scaling_dynamic": my_predbat.load_scaling_dynamic,
        "manual_load_adjust": my_predbat.manual_load_adjust,
        "now_utc": my_predbat.now_utc,
        "midnight_utc": my_predbat.midnight_utc,
        "minutes_now": my_predbat.minutes_now,
    }

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
        my_predbat.load_scaling = 1.05
        my_predbat.load_scaling_dynamic = {}
        my_predbat.manual_load_adjust = {}

        midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        my_predbat.midnight_utc = midnight_utc

        load_minutes = build_cumulative(0.02, 3000)  # 0.02 kWh/min -> 0.1 kWh per 5-min bucket
        load_forecast = {}
        import_minutes = build_cumulative(0.0, 3000)

        # Same day, same underlying load model, only minutes_now differs: at midnight (nothing
        # elapsed yet) and again mid-afternoon (most of the day elapsed).
        today_at_midnight = None
        today_mid_afternoon = None
        for minutes_now in (0, 900):  # 00:00 and 15:00
            my_predbat.now_utc = midnight_utc + timedelta(minutes=minutes_now)
            my_predbat.minutes_now = minutes_now
            my_predbat.load_today_comparison(load_minutes, load_forecast, {}, import_minutes, minutes_now=minutes_now, step=5, save=True)
            attrs = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["attributes"]
            state = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_predicted"]["state"]
            if minutes_now == 0:
                today_at_midnight = attrs["today"]
                state_at_midnight = state
            else:
                today_mid_afternoon = attrs["today"]
                state_mid_afternoon = state

        if today_at_midnight <= 0:
            print("  ERROR: today total at midnight should be positive for this to be a meaningful test, got {}".format(today_at_midnight))
            failed = True
        elif abs(today_mid_afternoon - today_at_midnight) > 0.02:
            print("  ERROR: 'today' total drifted as minutes_now advanced - midnight {} vs 15:00 {} (load_scaling=1.05 constant throughout)".format(today_at_midnight, today_mid_afternoon))
            failed = True
        elif abs(state_mid_afternoon - state_at_midnight) > 0.02:
            print("  ERROR: state drifted as minutes_now advanced - midnight {} vs 15:00 {} (load_scaling=1.05 constant throughout)".format(state_at_midnight, state_mid_afternoon))
            failed = True
        else:
            print("  PASS: 'today' total and state stayed flat across the day ({} at 00:00, {} at 15:00)".format(today_at_midnight, today_mid_afternoon))
    finally:
        for key, value in saved.items():
            setattr(my_predbat, key, value)

    return failed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_load_today_comparison(my_predbat):
    """
    Unit tests for load_today_comparison() covering the None-guard fix
    for dp2() calls when filtered_today() returns None, the
    import-exceeds-load regression (batpred#4154, #2537), the
    load_scaling/load_scaling_dynamic/manual_load_adjust-not-applied
    regression (#4496), and the whole-day "today" total drifting as
    minutes_now advances (#4496 follow-up).
    """
    failed = False
    print("**** Running load_today_comparison tests ****")

    failed = _test_none_guard_no_crash(my_predbat, failed)
    failed = _test_import_exceeding_load_still_counted(my_predbat, failed) or failed
    failed = _test_load_scaling_applied_to_predicted(my_predbat, failed) or failed
    failed = _test_load_scaling_dynamic_and_manual_adjust_applied_to_predicted(my_predbat, failed) or failed
    failed = _test_predicted_today_stable_across_the_day(my_predbat, failed) or failed

    return failed
