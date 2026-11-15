# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import find_charge_rate, get_charge_rate_curve, get_discharge_rate_curve
from battery_rate_grid import manual_rate_grid, rate_grid_curve
from const import MINUTE_WATT


def grid_curve(soc_curve, temperature_curve, soc_max, max_rate, temperature):
    """The absolute charge ceiling curve the manual curves give at one temperature (rated = max_rate)."""
    return rate_grid_curve(manual_rate_grid(soc_curve, temperature_curve, soc_max, max_rate), temperature, rated=max_rate)


def test_find_charge_rate(my_predbat):
    failed = 0

    # 2025-01-20 04:41:34.362134: Inverter 0 Charge window will be: 2025-01-19 23:30:00+00:00 - 2025-01-20 05:30:00+00:00 - current soc 95 target 100
    # 2025-01-20 04:41:34.364437: Low Power mode: minutes left: 40 absolute: 50 SOC: 9.04 Target SOC: 9.52 Charge left: 0.48 Max rate: 2600.0 Min rate: 576.0 Best rate: 2600.0 Best rate real: 1274.0 Battery temp 17.0
    # 2025-01-20 04:41:34.364497: Inverter 0 Target SOC 100 (this inverter 100.0) Battery temperature 17.0 Select charge rate 2600w (real 1274.0w) current charge rate 952
    # 2025-01-20 04:41:34.364549: Inverter 0 current charge rate is 952W and new target is 2600W
    # 2025-01-20 04:41:34.695878: Inverter 0 set charge rate 2600 via REST successful on retry 0

    current_charge_rate = 952
    soc = 9.04
    soc_max = 9.52
    log_to = print  # my_predbat.log
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    target_soc = soc_max
    battery_charge_power_curve = {100: 0.15, 99: 0.15, 98: 0.23, 97: 0.3, 96: 0.42, 95: 0.49, 94: 0.55, 93: 0.69, 92: 0.79, 91: 0.89, 90: 0.96}
    battery_charge_power_curve = my_predbat.validate_curve(battery_charge_power_curve, "test_charge_power_curve")
    set_charge_low_power = True
    charge_low_power_margin = my_predbat.charge_low_power_margin
    battery_rate_min = 0
    battery_rate_max_scaling = 1
    battery_loss = 0.96
    battery_temperature = 17.0
    battery_temperature_curve = {19: 0.33, 18: 0.33, 17: 0.33, 16: 0.33, 15: 0.33, 14: 0.33, 13: 0.33, 12: 0.33, 11: 0.33, 10: 0.25, 9: 0.25, 8: 0.25, 7: 0.25, 6: 0.25, 5: 0.25, 4: 0.25, 3: 0.25, 2: 0.25, 1: 0.15, 0: 0.00}
    battery_temperature_curve = my_predbat.validate_curve(battery_temperature_curve, "test_temperature_curve")
    max_rate = 2500

    best_rate, best_rate_real = find_charge_rate(
        minutes_now,
        soc,
        window,
        target_soc,
        max_rate / MINUTE_WATT,
        soc_max,
        grid_curve(battery_charge_power_curve, battery_temperature_curve, soc_max, max_rate / MINUTE_WATT, battery_temperature),
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        current_charge_rate=current_charge_rate / MINUTE_WATT,
    )
    print("Best_rate {} Best_rate_real {}".format(best_rate * MINUTE_WATT, best_rate_real * MINUTE_WATT))
    if best_rate * MINUTE_WATT != 2500:
        print("**** ERROR: Best rate should be 2500 ****")
        failed = 1
    if best_rate_real * MINUTE_WATT != 1225:
        print("**** ERROR: Best real rate should be 1225 ****")
        failed = 1
    return failed


def test_find_charge_rate_pv_overlap(my_predbat):
    """
    Test that low power charging is bypassed when the charge window overlaps with PV production

    Throttling the charge rate while the sun is shining stops PV going into the battery, the surplus
    is exported cheaply and the target is then made up with grid import, which increases the plan cost.

    set_charge_low_power_solar_full_rate turns that bypass off for a window where the trade does not
    hold - a free or very cheap import period, where the spilled PV costs nothing to buy back and the
    throttled rate is wanted anyway (#4975).
    """
    failed = 0

    log_to = print
    minutes_now = my_predbat.minutes_now
    soc = 1.0
    soc_max = 10.0
    target_soc = 5.0
    # Plenty of time left in the window so low power mode has room to slow the charge right down
    window = {"start": minutes_now - 30, "end": minutes_now + 300}
    # Flat curves so the rate is not capped by the battery or its temperature
    battery_charge_power_curve = my_predbat.validate_curve({100: 1.0, 99: 1.0, 98: 1.0, 97: 1.0, 96: 1.0, 95: 1.0, 94: 1.0, 93: 1.0, 92: 1.0, 91: 1.0, 90: 1.0}, "test_flat_charge_curve")
    battery_temperature_curve = my_predbat.validate_curve({20: 1.0, 19: 1.0, 18: 1.0, 17: 1.0, 16: 1.0, 15: 1.0}, "test_flat_temperature_curve")
    max_rate = 3000

    args = [
        minutes_now,
        soc,
        window,
        target_soc,
        max_rate / MINUTE_WATT,
        soc_max,
        grid_curve(battery_charge_power_curve, battery_temperature_curve, soc_max, max_rate / MINUTE_WATT, 17.0),
        True,
        my_predbat.charge_low_power_margin,
        0,
        1,
        0.96,
        log_to,
    ]
    kwargs = {"current_charge_rate": max_rate / MINUTE_WATT, "low_power_pv_threshold_w": my_predbat.low_power_pv_threshold_w}

    # Without PV low power mode should slow the charge down below the max rate
    dark_rate, dark_rate_real = find_charge_rate(*args, **kwargs)
    print("No PV - Best_rate {} Best_rate_real {}".format(dark_rate * MINUTE_WATT, dark_rate_real * MINUTE_WATT))
    if dark_rate * MINUTE_WATT >= max_rate:
        print("**** ERROR: Low power mode should reduce the rate below {}W when there is no PV, got {}W ****".format(max_rate, dark_rate * MINUTE_WATT))
        failed = 1

    # A trace of PV in the window is not enough to matter, low power mode should still apply
    trace_rate, trace_rate_real = find_charge_rate(*args, pv_window_kwh=0.05, **kwargs)
    print("Trace PV - Best_rate {} Best_rate_real {}".format(trace_rate * MINUTE_WATT, trace_rate_real * MINUTE_WATT))
    if trace_rate != dark_rate:
        print("**** ERROR: 0.05kWh of PV in the window should not change the rate, expected {}W got {}W ****".format(dark_rate * MINUTE_WATT, trace_rate * MINUTE_WATT))
        failed = 1

    # Real PV production in the window means we must charge at full rate so the PV is not throttled away
    sun_rate, sun_rate_real = find_charge_rate(*args, pv_window_kwh=2.0, **kwargs)
    print("With PV - Best_rate {} Best_rate_real {}".format(sun_rate * MINUTE_WATT, sun_rate_real * MINUTE_WATT))
    if sun_rate * MINUTE_WATT != max_rate:
        print("**** ERROR: PV production in the window should force the max rate {}W, got {}W ****".format(max_rate, sun_rate * MINUTE_WATT))
        failed = 1

    # With the full rate in solar switch off, the same PV must not force the max rate any more - the
    # window is throttled on its own charge_left / minutes_left just as a dark one would be (#4975)
    kwargs_no_full_rate = dict(kwargs)
    kwargs_no_full_rate["solar_full_rate"] = False
    opt_out_rate, opt_out_rate_real = find_charge_rate(*args, pv_window_kwh=2.0, **kwargs_no_full_rate)
    print("With PV, full rate in solar off - Best_rate {} Best_rate_real {}".format(opt_out_rate * MINUTE_WATT, opt_out_rate_real * MINUTE_WATT))
    if opt_out_rate != dark_rate:
        print("**** ERROR: with solar_full_rate off the PV should not change the rate, expected {}W got {}W ****".format(dark_rate * MINUTE_WATT, opt_out_rate * MINUTE_WATT))
        failed = 1

    # The switch only has meaning when there is PV to abandon low power for - with no PV in the window
    # it must make no difference either way
    no_pv_opt_out_rate, _ = find_charge_rate(*args, **kwargs_no_full_rate)
    if no_pv_opt_out_rate != dark_rate:
        print("**** ERROR: with no PV solar_full_rate should not change the rate, expected {}W got {}W ****".format(dark_rate * MINUTE_WATT, no_pv_opt_out_rate * MINUTE_WATT))
        failed = 1

    return failed


def test_find_charge_rate_string_temperature(my_predbat):
    """
    Test find_charge_rate with string temperature indices in the curve
    This tests backward compatibility with configs that may have string keys
    Uses extreme temperature curve values to ensure they actually affect the result
    """
    failed = 0

    current_charge_rate = 952
    soc = 5.0  # Lower SOC so more charge is needed
    soc_max = 9.52
    log_to = print  # my_predbat.log
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    target_soc = soc_max
    # Use a flat charge curve so temperature becomes the only limiting factor
    battery_charge_power_curve = {100: 1.0, 99: 1.0, 98: 1.0, 97: 1.0, 96: 1.0, 95: 1.0, 94: 1.0, 93: 1.0, 92: 1.0, 91: 1.0, 90: 1.0}
    battery_charge_power_curve = my_predbat.validate_curve(battery_charge_power_curve, "test_flat_charge_curve")
    set_charge_low_power = True
    charge_low_power_margin = my_predbat.charge_low_power_margin
    battery_rate_min = 0
    battery_rate_max_scaling = 1
    battery_loss = 0.96
    battery_temperature = 17.0
    # Temperature curve with STRING keys - these are scale factors that reduce charging rate
    # Using 1.0 (no reduction) for most temps, and 0.5 (50% reduction) at 17 degrees
    battery_temperature_curve = {"19": 1.0, "18": 1.0, "17": 0.5, "16": 1.0, "15": 1.0, "14": 1.0, "13": 1.0, "12": 1.0, "11": 1.0, "10": 1.0, "9": 1.0, "8": 1.0, "7": 1.0, "6": 1.0, "5": 1.0, "4": 1.0, "3": 1.0, "2": 1.0, "1": 1.0, "0": 1.0}
    battery_temperature_curve = my_predbat.validate_curve(battery_temperature_curve, "test_string_temperature_curve")
    max_rate = 6000  # High enough that temperature becomes the limiting factor

    best_rate, best_rate_real = find_charge_rate(
        minutes_now,
        soc,
        window,
        target_soc,
        max_rate / MINUTE_WATT,
        soc_max,
        grid_curve(battery_charge_power_curve, battery_temperature_curve, soc_max, max_rate / MINUTE_WATT, battery_temperature),
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        current_charge_rate=current_charge_rate / MINUTE_WATT,
    )
    print("String temp test - Best_rate {} Best_rate_real {}".format(best_rate * MINUTE_WATT, best_rate_real * MINUTE_WATT))
    # With temp scale factor 0.5 at 17 degrees and high max_rate, temperature should be the limiting factor
    # Temperature cap: 9.52 * 0.5 / 60 = 0.0793 kW/min = 4760W
    # Best rate should be capped by temperature to ~4760W
    if best_rate * MINUTE_WATT != 6000:
        print("**** ERROR: Best rate should be 6000 (with string temp curve) ****")
        failed = 1
    # Best_rate_real should be temperature-limited to ~4760W
    expected_temp_limited = int(9.52 * 0.5 / 60 * MINUTE_WATT)  # Should be ~4760W
    if abs(best_rate_real * MINUTE_WATT - expected_temp_limited) > 100:  # Allow 100W tolerance
        print("**** ERROR: Best real rate {} should be temp-limited to ~{}W ****".format(best_rate_real * MINUTE_WATT, expected_temp_limited))
        failed = 1
    return failed


def test_find_charge_rate_string_charge_curve(my_predbat):
    """
    Test find_charge_rate with string charge power curve indices
    This tests backward compatibility with configs that may have string keys in charge curve
    """
    failed = 0

    current_charge_rate = 952
    soc = 9.04
    soc_max = 9.52
    log_to = print  # my_predbat.log
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    target_soc = soc_max
    # Battery charge power curve with STRING keys instead of integers
    battery_charge_power_curve = {"100": 0.15, "99": 0.15, "98": 0.23, "97": 0.3, "96": 0.42, "95": 0.49, "94": 0.55, "93": 0.69, "92": 0.79, "91": 0.89, "90": 0.96}
    battery_charge_power_curve = my_predbat.validate_curve(battery_charge_power_curve, "test_string_charge_curve")
    set_charge_low_power = True
    charge_low_power_margin = my_predbat.charge_low_power_margin
    battery_rate_min = 0
    battery_rate_max_scaling = 1
    battery_loss = 0.96
    battery_temperature = 17.0
    battery_temperature_curve = {19: 0.33, 18: 0.33, 17: 0.33, 16: 0.33, 15: 0.33, 14: 0.33, 13: 0.33, 12: 0.33, 11: 0.33, 10: 0.25, 9: 0.25, 8: 0.25, 7: 0.25, 6: 0.25, 5: 0.25, 4: 0.25, 3: 0.25, 2: 0.25, 1: 0.15, 0: 0.00}
    battery_temperature_curve = my_predbat.validate_curve(battery_temperature_curve, "test_int_temperature_curve")
    max_rate = 2500

    best_rate, best_rate_real = find_charge_rate(
        minutes_now,
        soc,
        window,
        target_soc,
        max_rate / MINUTE_WATT,
        soc_max,
        grid_curve(battery_charge_power_curve, battery_temperature_curve, soc_max, max_rate / MINUTE_WATT, battery_temperature),
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        current_charge_rate=current_charge_rate / MINUTE_WATT,
    )
    print("String charge curve test - Best_rate {} Best_rate_real {}".format(best_rate * MINUTE_WATT, best_rate_real * MINUTE_WATT))
    # Should get the same results as with integer keys
    if best_rate * MINUTE_WATT != 2500:
        print("**** ERROR: Best rate should be 2500 (with string charge curve) ****")
        failed = 1
    if best_rate_real * MINUTE_WATT != 1225:
        print("**** ERROR: Best real rate should be 1225 (with string charge curve) ****")
        failed = 1
    return failed


def test_rate_grid_ceiling(my_predbat):
    """
    The grid ceiling is absolute: a cold 1 kW limit is 1 kW on the AC, DC-charge and export paths alike, a request below
    it is never scaled down, and a learned grid that tapers earlier when cold changes the low-power charge rate.
    """
    failed = 0
    rated = 5000 / MINUTE_WATT
    cold = manual_rate_grid({}, {5: 0.1, 20: 1.0}, 10.0, rated)  # 0.1C of 10 kWh = 1 kW at 5 degrees
    curve = rate_grid_curve(cold, 5, rated=rated)
    # A manual cold cap is absolute on every path; a learned grid on a DC path above the rated rate scales instead (history only measures up to the rated rate)
    for name, rate_max, expect in (("AC", rated, 1000), ("DC 4kW", 4000 / MINUTE_WATT, 1000), ("DC 7kW", 7000 / MINUTE_WATT, 1000)):
        value = get_charge_rate_curve(5.0, rate_max, 10.0, rate_max, curve, 0) * MINUTE_WATT
        if abs(value - expect) > 1e-6:
            print("**** ERROR: {} charge ceiling should be {}W, got {} ****".format(name, expect, value))
            failed = 1
    export = get_discharge_rate_curve(5.0, 3600 / MINUTE_WATT, 10.0, 3600 / MINUTE_WATT, curve, 0) * MINUTE_WATT
    if abs(export - 1000) > 1e-6:
        print("**** ERROR: export ceiling should be 1000W, got {} ****".format(export))
        failed = 1
    small = get_charge_rate_curve(5.0, 200 / MINUTE_WATT, 10.0, rated, curve, 0) * MINUTE_WATT
    if abs(small - 200) > 1e-6:
        print("**** ERROR: a 200W request should stay 200W, got {} ****".format(small))
        failed = 1
    learned = {"temp_low": 5, "rows": [[0.2] * 101], "zero_row": [0.2] * 101}
    scaled = get_charge_rate_curve(5.0, 7000 / MINUTE_WATT, 10.0, 7000 / MINUTE_WATT, rate_grid_curve(learned, 5, rated=rated), 0) * MINUTE_WATT
    if abs(scaled - 1400) > 1e-6:
        print("**** ERROR: a learned 0.2 row on a 7000W DC path above the 5000W rated rate scales to 1400W, got {} ****".format(scaled))
        failed = 1
    warm = get_charge_rate_curve(5.0, 7000 / MINUTE_WATT, 10.0, 7000 / MINUTE_WATT, rate_grid_curve(cold, 25, rated=rated), 0) * MINUTE_WATT
    if abs(warm - 7000) > 1e-6:
        print("**** ERROR: warm, a 7000W DC path is not held to the 5000W rated AC rate, got {} ****".format(warm))
        failed = 1

    # A learned grid: at 10 degrees the taper starts at 50% SoC, at 11 degrees and warmer it does not
    tapered = [1.0] * 50 + [0.3] * 51
    grid = {"temp_low": 10, "rows": [tapered, [1.0] * 101], "zero_row": [1.0] * 101}
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 30, "end": minutes_now + 60}  # 3 kWh in an hour needs more than the cold 1.5 kW ceiling
    rates = {}
    for temperature in (10, 20):
        rates[temperature] = find_charge_rate(minutes_now, 6.0, window, 9.0, rated, 10.0, rate_grid_curve(grid, temperature, rated=rated), True, my_predbat.charge_low_power_margin, 0, 1, 1.0, None, current_charge_rate=rated)
    if rates[10][1] >= rates[20][1]:
        print("**** ERROR: the cold row should give a lower real rate, got {} at 10C and {} at 20C ****".format(rates[10], rates[20]))
        failed = 1
    return failed
