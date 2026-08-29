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
The in-day load adjustment must carry across midnight rather than being reset to 1.0.

load_today_comparison() already seeds tomorrow's factor from today's final value with full weight
for the first three hours of the day (the yesterday_adjustment blend in output.py), so a plan that
assumes 1.0 past midnight contradicts what Predbat itself will do a few hours later - which is what
oversizes the overnight charge.
"""

from const import PREDICT_STEP

MINUTES_DAY = 24 * 60


def expected_carry(minute_absolute, scale_today):
    """The factor a slot should see: today in full, tomorrow decaying on the yesterday_weight curve, then 1.0."""
    day_offset = minute_absolute // MINUTES_DAY
    if day_offset == 0:
        return scale_today
    if day_offset > 1:
        return 1.0
    tod = minute_absolute % MINUTES_DAY
    weight = 1.0 if tod < 180 else (MINUTES_DAY - tod) / MINUTES_DAY
    return 1.0 + (scale_today - 1.0) * weight


def measure_factor(my_predbat, minutes_now, scale_today, minute):
    """Run step_data_history over a flat 1.0 kWh/minute series and recover the factor applied at a slot."""
    flat = {i: 1.0 for i in range(0, 4 * MINUTES_DAY)}
    values = my_predbat.step_data_history(flat, minutes_now, forward=True, scale_today=scale_today, scale_fixed=1.0)
    return values[minute] / float(PREDICT_STEP)


def test_inday_adjustment_carry(my_predbat):
    """
    Test that step_data_history carries the in-day adjustment over midnight on the same decay curve
    load_today_comparison will use tomorrow, instead of resetting it to 1.0.
    """
    print("**** Running inday_adjustment_carry tests ****")
    failed = False

    original_forecast_minutes = my_predbat.forecast_minutes
    original_plan_interval = my_predbat.plan_interval_minutes
    original_minutes_now = my_predbat.minutes_now
    my_predbat.forecast_minutes = 3 * MINUTES_DAY
    my_predbat.plan_interval_minutes = 30
    my_predbat.minutes_now = 0
    scale_today = 0.7

    # ---------------------------------------------------------------
    # Test 1: today keeps the full adjustment
    # ---------------------------------------------------------------
    print("Test 1: today unchanged")
    factor = measure_factor(my_predbat, 0, scale_today, 1200)
    if abs(factor - scale_today) > 1e-4:
        print("ERROR: today's slot factor {:.4f} != {:.4f}".format(factor, scale_today))
        failed = True
    else:
        print("Today keeps the full in-day adjustment ({:.4f})".format(factor))

    # ---------------------------------------------------------------
    # Test 2: the adjustment survives midnight and is continuous across it
    # ---------------------------------------------------------------
    print("Test 2: continuous across midnight")
    before = measure_factor(my_predbat, 0, scale_today, MINUTES_DAY - PREDICT_STEP)
    after = measure_factor(my_predbat, 0, scale_today, MINUTES_DAY + PREDICT_STEP)
    if abs(after - 1.0) < 1e-6:
        print("ERROR: the in-day adjustment was reset to 1.0 immediately after midnight")
        failed = True
    elif abs(before - after) > 1e-3:
        print("ERROR: discontinuity across midnight, {:.4f} -> {:.4f}".format(before, after))
        failed = True
    else:
        print("Adjustment carries continuously across midnight ({:.4f} -> {:.4f})".format(before, after))

    # ---------------------------------------------------------------
    # Test 3: full weight through the overnight charge window, then decaying
    # ---------------------------------------------------------------
    print("Test 3: decay curve over tomorrow")
    for minute in (MINUTES_DAY, MINUTES_DAY + 120, MINUTES_DAY + 360, MINUTES_DAY + 720, MINUTES_DAY + 1200):
        factor = measure_factor(my_predbat, 0, scale_today, minute)
        want = expected_carry(minute, scale_today)
        if abs(factor - want) > 1e-3:
            print("ERROR: minute {} factor {:.4f} != expected {:.4f}".format(minute, factor, want))
            failed = True
    if not failed:
        overnight = measure_factor(my_predbat, 0, scale_today, MINUTES_DAY + 120)
        midday = measure_factor(my_predbat, 0, scale_today, MINUTES_DAY + 720)
        print("Tomorrow decays on the yesterday_weight curve (02:00 {:.4f}, 12:00 {:.4f})".format(overnight, midday))

    # ---------------------------------------------------------------
    # Test 4: the day after tomorrow is back to neutral
    # ---------------------------------------------------------------
    print("Test 4: day two is neutral")
    factor = measure_factor(my_predbat, 0, scale_today, 2 * MINUTES_DAY + 600)
    if abs(factor - 1.0) > 1e-4:
        print("ERROR: day-two factor {:.4f} != 1.0".format(factor))
        failed = True
    else:
        print("Day after tomorrow returns to a neutral factor")

    # ---------------------------------------------------------------
    # Test 5: the same curve applies when the plan starts mid-day
    # ---------------------------------------------------------------
    print("Test 5: mid-day plan start")
    minutes_now = 900  # 15:00
    my_predbat.forecast_minutes = 2 * MINUTES_DAY
    for minute in (0, 300, MINUTES_DAY - minutes_now, MINUTES_DAY - minutes_now + 300):
        factor = measure_factor(my_predbat, minutes_now, scale_today, minute)
        want = expected_carry(minute + minutes_now, scale_today)
        if abs(factor - want) > 1e-3:
            print("ERROR: mid-day start minute {} (absolute {}) factor {:.4f} != expected {:.4f}".format(minute, minute + minutes_now, factor, want))
            failed = True
    if not failed:
        print("Mid-day plan start follows the same absolute-time curve")

    # ---------------------------------------------------------------
    # Test 6: an adjustment of 1.0 leaves every slot untouched
    # ---------------------------------------------------------------
    print("Test 6: neutral adjustment is a no-op")
    my_predbat.forecast_minutes = 3 * MINUTES_DAY
    for minute in (600, MINUTES_DAY + 600, 2 * MINUTES_DAY + 600):
        factor = measure_factor(my_predbat, 0, 1.0, minute)
        if abs(factor - 1.0) > 1e-6:
            print("ERROR: neutral adjustment changed minute {} to {:.4f}".format(minute, factor))
            failed = True
    if not failed:
        print("A neutral in-day adjustment leaves the whole horizon untouched")

    my_predbat.forecast_minutes = original_forecast_minutes
    my_predbat.plan_interval_minutes = original_plan_interval
    my_predbat.minutes_now = original_minutes_now

    return failed
