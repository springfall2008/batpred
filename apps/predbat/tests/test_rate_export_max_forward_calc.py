# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def _check_range(result, start, end, expected, label):
    """Assert every minute in [start, end) has the expected value."""
    failed = 0
    for minute in range(start, end):
        got = result.get(minute)
        if got != expected:
            print("ERROR: {}: minute {} expected {} got {}".format(label, minute, expected, got))
            failed = 1
            break
    return failed


def test_rate_export_max_forward_calc(my_predbat):
    """
    Tests for rate_export_max_forward_calc, the export mirror of rate_min_forward_calc.

    The function builds a forward-filled rate array from minute 0 to
    forecast_minutes + minutes_now + 48*60, then for every minute in
    [minutes_now, forecast_minutes + 24*60 + minutes_now) returns the
    maximum rate from that minute to the end of the array.
    """
    failed = 0

    old_forecast_minutes = my_predbat.forecast_minutes
    old_minutes_now = my_predbat.minutes_now

    my_predbat.forecast_minutes = 24 * 60
    my_predbat.minutes_now = 12 * 60

    output_start = my_predbat.minutes_now
    output_end = my_predbat.forecast_minutes + 24 * 60 + my_predbat.minutes_now
    total = my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60

    # --- Test 1: flat rate throughout ---
    print("*** rate_export_max_forward_calc test 1: flat rate")
    rates = {m: 15.0 for m in range(0, total)}
    result = my_predbat.rate_export_max_forward_calc(rates)
    failed |= _check_range(result, output_start, output_end, 15.0, "flat rate")

    # --- Test 2: a single high slot in the future is seen by every earlier minute, and by none after ---
    # This is the saving-session shape: a short spike that must stop counting once it has passed.
    print("*** rate_export_max_forward_calc test 2: high slot in future")
    high_minute = my_predbat.minutes_now + 6 * 60
    rates = {m: 15.0 for m in range(0, total)}
    rates[high_minute] = 100.0
    result = my_predbat.rate_export_max_forward_calc(rates)
    failed |= _check_range(result, output_start, high_minute, 100.0, "before high_minute")
    if result.get(high_minute) != 100.0:
        print("ERROR: test 2: at high_minute {} expected 100.0 got {}".format(high_minute, result.get(high_minute)))
        failed = 1
    failed |= _check_range(result, high_minute + 1, output_end, 15.0, "after high_minute")

    # --- Test 3: strictly decreasing rates → max forward equals the rate at each minute ---
    print("*** rate_export_max_forward_calc test 3: decreasing rates")
    rates = {m: float(total - m) for m in range(total)}
    result = my_predbat.rate_export_max_forward_calc(rates)
    for minute in range(output_start, output_end):
        if result.get(minute) != float(total - minute):
            print("ERROR: test 3: minute {} expected {} got {}".format(minute, float(total - minute), result.get(minute)))
            failed = 1
            break

    # --- Test 4: forward-fill — a gap in the rates dict carries the last known rate ---
    print("*** rate_export_max_forward_calc test 4: forward fill")
    rates = {0: 5.0, output_start + 2 * 60: 20.0}
    result = my_predbat.rate_export_max_forward_calc(rates)
    failed |= _check_range(result, output_start, output_start + 2 * 60, 20.0, "forward fill before rise")
    if result.get(output_start + 2 * 60) != 20.0:
        print("ERROR: test 4: at rise minute expected 20.0 got {}".format(result.get(output_start + 2 * 60)))
        failed = 1

    # --- Test 5: output keys are exactly [minutes_now, forecast_minutes + 24*60 + minutes_now) ---
    print("*** rate_export_max_forward_calc test 5: output key range")
    rates = {m: 15.0 for m in range(total)}
    result = my_predbat.rate_export_max_forward_calc(rates)
    expected_keys = set(range(output_start, output_end))
    if set(result.keys()) != expected_keys:
        extra = set(result.keys()) - expected_keys
        missing = expected_keys - set(result.keys())
        print("ERROR: test 5: extra keys {} missing keys {}".format(sorted(extra)[:5], sorted(missing)[:5]))
        failed = 1

    # --- Test 6: an empty rates dict yields zero throughout rather than raising ---
    print("*** rate_export_max_forward_calc test 6: no export rates")
    result = my_predbat.rate_export_max_forward_calc({})
    failed |= _check_range(result, output_start, output_end, 0.0, "no export rates")

    # --- Test 7: fetch builds it from the base export tariff, before saving sessions are layered on ---
    # A session inflating self.rate_export must not reach rate_export_max_forward, which is what makes
    # the export-recovery haircut in battery_value_rate immune to one-off high-price events.
    print("*** rate_export_max_forward_calc test 7: built from base rates, not session-inflated ones")
    saved = {attr: getattr(my_predbat, attr, None) for attr in ("rate_export", "rate_export_base", "rate_export_max_forward")}
    my_predbat.rate_export_base = {m: 0.0 for m in range(total)}
    my_predbat.rate_export = {m: (100.0 if m < my_predbat.minutes_now + 60 else 0.0) for m in range(total)}
    my_predbat.rate_export_max_forward = my_predbat.rate_export_max_forward_calc(my_predbat.rate_export_base)
    sample = my_predbat.rate_export_max_forward.get(my_predbat.minutes_now)
    if sample != 0.0:
        print("ERROR: test 7: rate_export_max_forward {} picked up the saving session, expected the base 0.0".format(sample))
        failed = 1
    for attr, value in saved.items():
        setattr(my_predbat, attr, value)

    # Restore
    my_predbat.forecast_minutes = old_forecast_minutes
    my_predbat.minutes_now = old_minutes_now

    return failed
