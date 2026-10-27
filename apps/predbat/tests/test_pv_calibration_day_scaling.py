# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""PV calibration's per-day scaling must hold a day's calibrated total at raw forecast x total adjustment.

pv_calibration applies a per-slot adjustment and then a per-day factor, days_use_scaling, chosen so that the
day's calibrated total comes back to the raw total times total_adjustment. The factor is worked out from two
per-day sums of the per-minute forecast (kWh per minute): the raw sum and the slot-adjusted sum.

GH#5690: those sums were rounded to 2 dp on every per-minute addition. On a dull day every minute is below
0.01 kWh (anything under a 0.6 kW average), so each addition rounded to +0.00 or +0.01 and both sums just counted
minutes at or above 0.005 kWh. The day factor then came out wrong - by 1.84x on the reporter's system - and the
planner expected far more (or less) PV than the forecast said on exactly the days calibration matters most.
"""
from tests.test_pv_calibration_clipping import bell, calibrate, check, flat

# The history the calibration learns from: a 4 kW clear-sky week that delivered 85% of its forecast, so every
# slot adjustment and the total adjustment are near 0.85. Generation stays well below any inverter limit.
HISTORY_ADJUSTMENT = 0.85


def history_forecast(minute_of_day):
    """kW forecast for the history week at this minute of the day."""
    return bell(4.0)(minute_of_day)


def history_actual(minute_of_day):
    """kW generated in the history week at this minute of the day."""
    return HISTORY_ADJUSTMENT * bell(4.0)(minute_of_day)


def day_total_kwh(profile_kw):
    """Return the kWh for a day given a dict of kW by minute of day."""
    return sum(profile_kw.values()) / 60.0


def check_day_total(name, future_forecast):
    """Calibrate against the history week and check tomorrow's calibrated total is raw x total_adjustment."""
    tomorrow, total_adjustment, _ = calibrate(history_forecast, history_actual, future_forecast, inverter_limit_kw=20.0)
    raw_kwh = sum(future_forecast(minute_of_day) for minute_of_day in range(1440)) / 60.0
    calibrated_kwh = day_total_kwh(tomorrow)
    expected_kwh = raw_kwh * total_adjustment
    print("  {}: raw {:.2f} kWh, total_adjustment {}, calibrated {:.2f} kWh, expected {:.2f} kWh".format(name, raw_kwh, total_adjustment, calibrated_kwh, expected_kwh))
    failed = check("{} calibrated day total against raw x total_adjustment".format(name), calibrated_kwh, expected_kwh, 0.01)
    failed |= check("{} total_adjustment".format(name), total_adjustment, HISTORY_ADJUSTMENT, 0.03)
    return failed


def test_dull_day_not_inflated():
    """A dull clear-sky shaped day peaking at 0.4 kW (every minute under 0.01 kWh) calibrates to raw x adjustment, not well above it."""
    return check_day_total("dull 0.4 kW peak day", bell(0.4))


def test_flat_dull_day_not_deflated():
    """A flat 0.4 kW day, where every minute and every adjusted minute rounds up to 0.01 kWh, is not scaled by the adjustment twice."""
    return check_day_total("flat 0.4 kW day", flat(0.4))


def test_bright_day_unchanged():
    """A bright 4 kW peak day, which the rounding barely touched, still calibrates to raw x adjustment."""
    return check_day_total("bright 4 kW peak day", bell(4.0))


def run_pv_calibration_day_scaling_tests(my_predbat):
    """Run the PV calibration per-day scaling tests. Returns True on failure."""
    print("**** Running PV calibration per-day scaling tests ****")
    failed = False
    failed |= test_dull_day_not_inflated()
    failed |= test_flat_dull_day_not_deflated()
    failed |= test_bright_day_unchanged()
    if not failed:
        print("**** PV calibration per-day scaling tests passed ****")
    return failed
