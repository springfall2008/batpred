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


def test_rate_min_forward_calc(my_predbat):
    """
    Tests for rate_min_forward_calc.

    The function builds a forward-filled rate array from minute 0 to
    forecast_minutes + minutes_now + 48*60, then for every minute in
    [minutes_now, forecast_minutes + 24*60 + minutes_now) returns the
    minimum rate from that minute to the end of the array.
    """
    failed = 0

    old_forecast_minutes = my_predbat.forecast_minutes
    old_minutes_now = my_predbat.minutes_now
    old_rate_min = my_predbat.rate_min

    my_predbat.forecast_minutes = 24 * 60
    my_predbat.minutes_now = 12 * 60
    my_predbat.rate_min = 0.0

    output_start = my_predbat.minutes_now
    output_end = my_predbat.forecast_minutes + 24 * 60 + my_predbat.minutes_now

    # --- Test 1: flat rate throughout ---
    print("*** rate_min_forward_calc test 1: flat rate")
    rates = {m: 10.0 for m in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60)}
    result = my_predbat.rate_min_forward_calc(rates)
    failed |= _check_range(result, output_start, output_end, 10.0, "flat rate")

    # --- Test 2: lower rate in the future → every minute sees that future minimum ---
    print("*** rate_min_forward_calc test 2: lower rate in future")
    low_minute = my_predbat.minutes_now + 6 * 60   # 6 hours from now
    rates = {m: 10.0 for m in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60)}
    rates[low_minute] = 2.0
    result = my_predbat.rate_min_forward_calc(rates)
    # Every minute from output_start up to (but not including) low_minute should see 2.0
    failed |= _check_range(result, output_start, low_minute, 2.0, "before low_minute")
    # At low_minute the rate is 2.0 and nothing lower follows
    if result.get(low_minute) != 2.0:
        print("ERROR: test 2: at low_minute {} expected 2.0 got {}".format(low_minute, result.get(low_minute)))
        failed = 1
    # After low_minute back to 10.0
    failed |= _check_range(result, low_minute + 1, output_end, 10.0, "after low_minute")

    # --- Test 3: rates strictly increasing → min forward equals the rate at each minute ---
    print("*** rate_min_forward_calc test 3: increasing rates")
    rates = {}
    for m in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60):
        rates[m] = float(m)
    result = my_predbat.rate_min_forward_calc(rates)
    # Each minute's min-forward is its own rate (everything later is higher)
    for minute in range(output_start, output_end):
        if result.get(minute) != float(minute):
            print("ERROR: test 3: minute {} expected {} got {}".format(minute, float(minute), result.get(minute)))
            failed = 1
            break

    # --- Test 4: forward-fill — gap in rates dict uses last known rate ---
    print("*** rate_min_forward_calc test 4: forward fill")
    my_predbat.rate_min = 5.0
    rates = {0: 5.0, output_start + 2 * 60: 3.0}   # rate drops at output_start+2h
    result = my_predbat.rate_min_forward_calc(rates)
    # Before the drop the forward-looking min is 3.0
    failed |= _check_range(result, output_start, output_start + 2 * 60, 3.0, "forward fill before drop")
    # From the drop onwards, min is 3.0
    if result.get(output_start + 2 * 60) != 3.0:
        print("ERROR: test 4: at drop minute expected 3.0 got {}".format(result.get(output_start + 2 * 60)))
        failed = 1
    my_predbat.rate_min = 0.0

    # --- Test 5: output keys are exactly [minutes_now, forecast_minutes + 24*60 + minutes_now) ---
    print("*** rate_min_forward_calc test 5: output key range")
    rates = {m: 10.0 for m in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60)}
    result = my_predbat.rate_min_forward_calc(rates)
    expected_keys = set(range(output_start, output_end))
    if set(result.keys()) != expected_keys:
        extra = set(result.keys()) - expected_keys
        missing = expected_keys - set(result.keys())
        print("ERROR: test 5: extra keys {} missing keys {}".format(sorted(extra)[:5], sorted(missing)[:5]))
        failed = 1

    # --- Test 6: minimum is at the very last minute of the array ---
    print("*** rate_min_forward_calc test 6: minimum at end of array")
    total = my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60
    rates = {m: 10.0 for m in range(total)}
    last_minute = total - 1
    rates[last_minute] = 1.0
    result = my_predbat.rate_min_forward_calc(rates)
    # Every minute in the output range should see 1.0 as the forward minimum
    failed |= _check_range(result, output_start, output_end, 1.0, "min at end of array")

    # --- Test 7: rate_scan leaves rate_min_forward alone ---
    # rate_min_forward is the replacement cost battery_value_rate gives a kWh left in the battery, so
    # fetch_sensor_data() builds it from the tariff before the saving/free/Axle sessions go on. The final
    # rate_scan sees the rates with the sessions in, and rebuilding the forward minimum there let a 0p
    # free session tomorrow afternoon drag the value of every stored kWh down to the 1p floor.
    print("*** rate_min_forward_calc test 7: rate_scan does not rebuild rate_min_forward from session rates")
    # rate_scan mutates several shared fields; snapshot and restore them all so later tests are unaffected.
    saved = {attr: getattr(my_predbat, attr, None) for attr in ("rate_import", "rate_min", "rate_max", "rate_average", "rate_min_minute", "rate_max_minute", "rate_min_forward")}
    total = my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60
    tariff_forward = my_predbat.rate_min_forward_calc({m: 10.0 for m in range(total)})
    my_predbat.rate_min_forward = dict(tariff_forward)
    session_rates = {m: 10.0 for m in range(total)}
    for minute in range(my_predbat.minutes_now + 6 * 60, my_predbat.minutes_now + 7 * 60):
        session_rates[minute] = 0.0
    my_predbat.rate_scan(session_rates, print=True)
    if my_predbat.rate_min_forward != tariff_forward:
        print("ERROR: test 7: rate_scan rebuilt rate_min_forward from rates with a free session in, now {} at minutes_now".format(my_predbat.rate_min_forward.get(my_predbat.minutes_now)))
        failed = 1
    if my_predbat.rate_min != 0.0:
        print("ERROR: test 7: rate_min should still see the free session (thresholds handle sessions themselves), got {}".format(my_predbat.rate_min))
        failed = 1
    for attr, value in saved.items():
        setattr(my_predbat, attr, value)

    # Restore
    my_predbat.forecast_minutes = old_forecast_minutes
    my_predbat.minutes_now = old_minutes_now
    my_predbat.rate_min = old_rate_min

    return failed
