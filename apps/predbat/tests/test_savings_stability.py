# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def test_savings_stability(my_predbat):
    """
    Test that compute_rate_low_for_yesterday (used in calculate_yesterday) returns a
    stable rate_low regardless of which point in the day the calculation runs.

    Bug: when past_rates was built with end_record + minutes_now entries, today's
    cheap/negative tariff slots (e.g. Agile 0p) entered the dict as minutes_now grew,
    dropping rate_low and causing rate_scan_window to find no charge windows for
    yesterday, making savings_yesterday fluctuate hourly.

    Fix: compute_rate_low_for_yesterday filters to k < end_record (yesterday only).
    This test calls the production methods directly (history_to_future_rates and
    compute_rate_low_for_yesterday) to verify the fix is exercised.
    """
    failed = 0
    end_record = 24 * 60  # 1440 minutes

    # Build rate_import dict representing:
    #   Yesterday (keys -1440..-1): cheap 00:00-06:00 (5p), expensive otherwise (25p)
    #   Today (keys 0+): expensive mostly, but a 0p slot at 60-120 min (like Agile)
    cheap_rate = 5.0
    expensive_rate = 25.0
    today_cheap_rate = 0.0

    rate_import = {}
    # Yesterday: negative keys -1440 to -1
    for minute in range(0, end_record):
        rate_import[minute - end_record] = cheap_rate if minute < 360 else expensive_rate
    # Today: keys 0 to 1439
    for minute in range(0, end_record):
        rate_import[minute] = today_cheap_rate if 60 <= minute < 120 else expensive_rate

    # Test compute_rate_low_for_yesterday (production helper) at three points in time.
    # Uses history_to_future_rates (production code) to build past_rates exactly as
    # calculate_yesterday() does.
    for minutes_now_val, label in [(0, "midnight"), (240, "mid-morning"), (720, "noon")]:
        past_rates = my_predbat.history_to_future_rates(rate_import, end_record, end_record + minutes_now_val)
        rate_low = my_predbat.compute_rate_low_for_yesterday(past_rates, end_record)
        print("rate_low at {} (minutes_now={}): {}".format(label, minutes_now_val, rate_low))
        if rate_low != cheap_rate:
            print("ERROR: rate_low at {} should be {} (yesterday's min), got {}".format(label, cheap_rate, rate_low))
            failed = 1

    # Confirm the old broken behaviour: min of the full dict drops when today's 0p slot is included
    past_rates_morning = my_predbat.history_to_future_rates(rate_import, end_record, end_record + 240)
    rate_low_broken_morning = min(past_rates_morning.values()) if past_rates_morning else 0.0
    print("OLD broken rate_low at mid-morning: {} (should be {}, confirms the bug)".format(rate_low_broken_morning, today_cheap_rate))
    if rate_low_broken_morning != today_cheap_rate:
        print("INFO: broken rate_low at morning={} (expected {} to demonstrate original bug)".format(rate_low_broken_morning, today_cheap_rate))

    # Test rate_scan_window behaviour with deterministic state, save/restoring all state
    old_combine_charge_slots = my_predbat.combine_charge_slots
    old_minutes_now = my_predbat.minutes_now
    old_forecast_minutes = my_predbat.forecast_minutes

    try:
        # Set deterministic values required by rate_scan_window / find_charge_window
        my_predbat.minutes_now = 0
        my_predbat.forecast_minutes = end_record
        my_predbat.combine_charge_slots = True

        # Build past_rates at noon (worst case: today's 0p slot is included)
        past_rates_noon = my_predbat.history_to_future_rates(rate_import, end_record, end_record + 720)
        rate_low_noon = my_predbat.compute_rate_low_for_yesterday(past_rates_noon, end_record)

        # Fixed rate_low (5p) should find yesterday's cheap window (0-360 minutes)
        charge_windows, _low, _high = my_predbat.rate_scan_window(past_rates_noon, 5, rate_low_noon, False, return_raw=True)
        charge_windows_yesterday = [c for c in charge_windows if c["start"] < end_record]
        print("Charge windows with FIXED rate_low={}: {}".format(rate_low_noon, charge_windows_yesterday))
        if not charge_windows_yesterday:
            print("ERROR: charge windows should be found in yesterday's data with fixed rate_low")
            failed = 1
        else:
            for cw in charge_windows_yesterday:
                if cw["average"] > rate_low_noon + 0.01:
                    print("ERROR: charge window average {} exceeds fixed rate_low {}".format(cw["average"], rate_low_noon))
                    failed = 1

        # Old broken rate_low (0p) finds no charge windows in yesterday's data
        rate_low_broken = min(past_rates_noon.values()) if past_rates_noon else 0.0
        charge_windows_broken, _, _ = my_predbat.rate_scan_window(past_rates_noon, 5, rate_low_broken, False, return_raw=True)
        charge_windows_broken_yesterday = [c for c in charge_windows_broken if c["start"] < end_record]
        print("Charge windows with BROKEN rate_low={}: {}".format(rate_low_broken, charge_windows_broken_yesterday))
        # With 0p threshold no yesterday windows should be found (yesterday min is 5p)
        if charge_windows_broken_yesterday:
            print("INFO: unexpected windows found at 0p threshold - may be harmless test setup artifact")

    finally:
        # Always restore shared my_predbat state
        my_predbat.combine_charge_slots = old_combine_charge_slots
        my_predbat.minutes_now = old_minutes_now
        my_predbat.forecast_minutes = old_forecast_minutes

    return failed
