# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import find_charge_rate
from const import MINUTE_WATT


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
        battery_charge_power_curve,
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        battery_temperature=battery_temperature,
        battery_temperature_curve=battery_temperature_curve,
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
        battery_charge_power_curve,
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        battery_temperature=battery_temperature,
        battery_temperature_curve=battery_temperature_curve,
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
        battery_charge_power_curve,
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        battery_temperature=battery_temperature,
        battery_temperature_curve=battery_temperature_curve,
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
