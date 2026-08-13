# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def _check(cond, message, failures):
    """Record a failure message if the condition is not met."""
    if not cond:
        print("ERROR: {}".format(message))
        failures.append(message)


def run_morning_cover_tests(my_predbat):
    """Run tests for the dynamic best_soc_max morning_cover mode. Returns True on failure."""
    print("**** Running morning cover tests ****")
    failures = []

    my_predbat.best_soc_max_mode = "morning_cover"
    my_predbat.best_soc_max = 0.0
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 1.0
    my_predbat.battery_loss_discharge = 0.8
    my_predbat.charge_window_best = [{"start": 0, "end": 60}]

    load_minutes_step = {60: 1.0, 65: 1.0, 70: 1.0, 75: 1.0}
    pv_forecast_minute_step = {60: 0.0, 65: 0.5, 70: 1.5, 75: 2.0}
    my_predbat.update_best_soc_max_morning_cover(load_minutes_step, pv_forecast_minute_step)
    _check(my_predbat.best_soc_max == 2.88, "morning_cover should cap reserve plus loss-adjusted deficit", failures)

    my_predbat.best_soc_max = 0.0
    load_minutes_step = {60: 1.0, 65: 1.0}
    pv_forecast_minute_step = {60: 0.0, 65: 0.0}
    my_predbat.update_best_soc_max_morning_cover(load_minutes_step, pv_forecast_minute_step)
    _check(my_predbat.best_soc_max == 0.0, "morning_cover should leave uncapped if PV never exceeds load", failures)

    my_predbat.best_soc_max_mode = None
    my_predbat.best_soc_max = 4.0
    my_predbat.update_best_soc_max_morning_cover({0: 1.0}, {0: 2.0})
    _check(my_predbat.best_soc_max == 4.0, "normal numeric best_soc_max should be unchanged", failures)

    return len(failures) > 0
