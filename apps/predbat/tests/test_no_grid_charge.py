# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the battery_charging_from_grid mode.

With the switch off the planner may only leave a charge window off or hold it at the reserve, because
any limit above the SoC is met from the grid in the prediction model. Solar charging is unaffected:
a hold takes PV, and an off window runs in ECO mode which soaks surplus anyway.
"""

from tests.test_infra import reset_inverter, reset_rates, update_rates_import, update_rates_export
from prediction import Prediction


def build_windows(my_predbat, price_cheap=5.0, price_peak=30.0):
    """Build 48 half-hour windows alternating a cheap overnight block with an expensive daytime one."""
    charge_window_best = []
    for n in range(0, 48):
        off_peak = (n % 24) > 12
        price = price_cheap if off_peak else price_peak
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
    return charge_window_best


def run_plan(my_predbat, charge_window_best, battery_charging_from_grid, load_amount=0.5, pv_amount=0.0, battery_size=10.0, battery_soc=5.0):
    """Drive a full window optimisation and return the resulting charge limits.

    Deliberately does not assert an exact plan the way run_optimise_all_windows does: these tests are
    about an invariant that must hold for every window, not about one particular optimiser outcome,
    so pinning exact limits would make them fail on unrelated tuning changes.
    """
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.reserve = 0.5
    my_predbat.set_charge_freeze = True
    my_predbat.best_soc_keep = 0.0
    my_predbat.debug_enable = False
    my_predbat.battery_charging_from_grid = battery_charging_from_grid

    export_window_best = []
    reset_rates(my_predbat, 10.0, 5.5)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    charge_limit_best = [0 for _ in range(len(charge_window_best))]
    export_limits_best = []
    metric, _, _, _, _, _, _, _, metric_keep, _, _ = my_predbat.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record)

    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    my_predbat.optimise_all_windows(metric, metric_keep)

    # Re-simulate the chosen plan so the returned SoC belongs to it, rather than to whichever internal
    # candidate simulation happened to run last
    final = my_predbat.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=end_record, save="best")
    final_soc = final[5]
    return my_predbat.charge_limit_best, my_predbat.charge_window_best, final_soc


def test_grid_charge_allowed_window(my_predbat):
    """allow_grid_charge_window gates on the switch and exempts only negative-rate windows."""
    print("  - test_grid_charge_allowed_window")
    failed = False
    windows = [{"start": 0, "end": 30, "average": 10.0}, {"start": 30, "end": 60, "average": -2.0}, {"start": 60, "end": 90, "average": 0.0}]
    saved = my_predbat.battery_charging_from_grid

    my_predbat.battery_charging_from_grid = True
    for window_n in range(len(windows)):
        if not my_predbat.allow_grid_charge_window(windows, window_n):
            print("ERROR: window {} should be allowed when the switch is on".format(window_n))
            failed = True

    my_predbat.battery_charging_from_grid = False
    expected = [False, True, False]  # only the negative-rate window is exempt; zero is not negative
    for window_n in range(len(windows)):
        got = my_predbat.allow_grid_charge_window(windows, window_n)
        if got != expected[window_n]:
            print("ERROR: window {} allowed should be {} got {}".format(window_n, expected[window_n], got))
            failed = True

    # A group of windows only qualifies if every member is negative
    if my_predbat.allow_grid_charge_window(windows, 0, all_n=[0, 1]):
        print("ERROR: a mixed group should not be exempt")
        failed = True
    if not my_predbat.allow_grid_charge_window(windows, 1, all_n=[1]):
        print("ERROR: an all-negative group should be exempt")
        failed = True

    my_predbat.battery_charging_from_grid = saved
    return failed


def test_no_grid_charge_plan(my_predbat):
    """With the switch off the planner never targets above the reserve; with it on, it does."""
    print("  - test_no_grid_charge_plan")
    failed = False
    reset_inverter(my_predbat)

    # Baseline: grid charging allowed, so the cheap overnight windows get used to fill the battery
    charge_limit_best, _, _ = run_plan(my_predbat, build_windows(my_predbat), battery_charging_from_grid=True)
    if not [limit for limit in charge_limit_best if limit > my_predbat.reserve]:
        print("ERROR: with grid charging allowed the planner should charge in at least one window")
        failed = True

    # Switch off: every window must now be off (0) or a hold (reserve), never a grid charge
    charge_limit_best, _, _ = run_plan(my_predbat, build_windows(my_predbat), battery_charging_from_grid=False)
    for window_n, limit in enumerate(charge_limit_best):
        if limit > my_predbat.reserve:
            print("ERROR: window {} has charge limit {} above reserve {} with grid charging off".format(window_n, limit, my_predbat.reserve))
            failed = True

    return failed


def test_no_grid_charge_keeps_solar(my_predbat):
    """Turning the mode off must not stop the battery filling from surplus solar."""
    print("  - test_no_grid_charge_keeps_solar")
    failed = False
    reset_inverter(my_predbat)

    # Plenty of PV, small load: the battery should still end the run well above where it started even
    # though no window may charge from the grid
    _, _, final_soc = run_plan(my_predbat, build_windows(my_predbat), battery_charging_from_grid=False, load_amount=0.2, pv_amount=3.0, battery_soc=1.0)
    if final_soc <= 1.0:
        print("ERROR: solar should still charge the battery with grid charging off, final SoC {}".format(final_soc))
        failed = True

    return failed


def test_no_grid_charge_negative_rate_exemption(my_predbat):
    """A negative import rate is the one case where the mode still permits a grid charge."""
    print("  - test_no_grid_charge_negative_rate_exemption")
    failed = False
    reset_inverter(my_predbat)

    # Half the windows are paid-to-import, the rest are expensive
    charge_window_best = []
    for n in range(0, 48):
        price = -10.0 if (n % 24) > 12 else 30.0
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})

    charge_limit_best, charge_window_out, _ = run_plan(my_predbat, charge_window_best, battery_charging_from_grid=False)

    charged = False
    for window_n, limit in enumerate(charge_limit_best):
        rate = charge_window_out[window_n]["average"]
        if limit > my_predbat.reserve:
            charged = True
            if rate >= 0:
                print("ERROR: window {} at rate {} charged above reserve despite grid charging being off".format(window_n, rate))
                failed = True
    if not charged:
        print("ERROR: negative-rate windows should still be allowed to charge from the grid")
        failed = True

    return failed


def test_manual_charge_downgraded(my_predbat):
    """A manual charge slot is downgraded to a hold rather than defeating the mode."""
    print("  - test_manual_charge_downgraded")
    failed = False
    saved_flag = my_predbat.battery_charging_from_grid
    saved_manual = my_predbat.manual_charge_times
    saved_limits = my_predbat.charge_limit_best
    saved_windows = my_predbat.charge_window_best
    saved_export_limits = my_predbat.export_limits_best
    saved_export_windows = my_predbat.export_window_best

    start = my_predbat.minutes_now
    my_predbat.charge_window_best = [{"start": start, "end": start + 30, "average": 10.0}]
    my_predbat.charge_limit_best = [0]
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.manual_charge_times = [start]
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True

    my_predbat.battery_charging_from_grid = True
    my_predbat.optimise_charge_windows_manual()
    if my_predbat.charge_limit_best[0] != my_predbat.soc_max:
        print("ERROR: manual charge should target soc_max {} got {}".format(my_predbat.soc_max, my_predbat.charge_limit_best[0]))
        failed = True

    my_predbat.charge_limit_best = [0]
    my_predbat.battery_charging_from_grid = False
    my_predbat.optimise_charge_windows_manual()
    if my_predbat.charge_limit_best[0] != my_predbat.reserve:
        print("ERROR: manual charge should be downgraded to reserve {} got {}".format(my_predbat.reserve, my_predbat.charge_limit_best[0]))
        failed = True

    # A negative-rate manual slot is still honoured in full
    my_predbat.charge_window_best = [{"start": start, "end": start + 30, "average": -5.0}]
    my_predbat.charge_limit_best = [0]
    my_predbat.optimise_charge_windows_manual()
    if my_predbat.charge_limit_best[0] != my_predbat.soc_max:
        print("ERROR: manual charge at a negative rate should still target soc_max got {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    my_predbat.battery_charging_from_grid = saved_flag
    my_predbat.manual_charge_times = saved_manual
    my_predbat.charge_limit_best = saved_limits
    my_predbat.charge_window_best = saved_windows
    my_predbat.export_limits_best = saved_export_limits
    my_predbat.export_window_best = saved_export_windows
    return failed


def test_is_freeze_charge_backstop(my_predbat):
    """execute's is_freeze_charge downgrades an import-requiring target when grid charging is off."""
    print("  - test_is_freeze_charge_backstop")
    failed = False
    saved_flag = my_predbat.battery_charging_from_grid
    saved_rates = my_predbat.rate_import
    saved_soc_max = my_predbat.soc_max
    saved_soc_percent = my_predbat.soc_percent
    saved_reserve_percent = my_predbat.reserve_percent

    my_predbat.soc_max = 10.0
    my_predbat.soc_percent = 50
    my_predbat.reserve_percent = 10
    my_predbat.rate_import = {my_predbat.minutes_now: 20.0}

    # Switch on: a target above SoC is a real charge, and only the reserve counts as a freeze
    my_predbat.battery_charging_from_grid = True
    if my_predbat.is_freeze_charge(8.0):
        print("ERROR: 80% target should be a real charge when grid charging is allowed")
        failed = True
    if not my_predbat.is_freeze_charge(1.0):
        print("ERROR: a limit at the reserve should always be a freeze")
        failed = True

    # Switch off: the same target is downgraded to a hold
    my_predbat.battery_charging_from_grid = False
    if not my_predbat.is_freeze_charge(8.0):
        print("ERROR: 80% target should be downgraded to a hold when grid charging is off")
        failed = True
    # A target at or below SoC needs no import, so it is left alone
    if my_predbat.is_freeze_charge(4.0):
        print("ERROR: a 40% target below the 50% SoC needs no import and should not be downgraded")
        failed = True
    # Negative rates are exempt even with the switch off
    my_predbat.rate_import = {my_predbat.minutes_now: -3.0}
    if my_predbat.is_freeze_charge(8.0):
        print("ERROR: a negative import rate should still permit a real charge")
        failed = True

    my_predbat.battery_charging_from_grid = saved_flag
    my_predbat.rate_import = saved_rates
    my_predbat.soc_max = saved_soc_max
    my_predbat.soc_percent = saved_soc_percent
    my_predbat.reserve_percent = saved_reserve_percent
    return failed


def run_no_grid_charge_tests(my_predbat):
    """Run every battery_charging_from_grid test."""
    print("**** Running no grid charge tests ****\n")
    failed = test_grid_charge_allowed_window(my_predbat)
    failed |= test_is_freeze_charge_backstop(my_predbat)
    failed |= test_manual_charge_downgraded(my_predbat)
    if failed:
        return failed
    failed |= test_no_grid_charge_plan(my_predbat)
    if failed:
        return failed
    failed |= test_no_grid_charge_keeps_solar(my_predbat)
    failed |= test_no_grid_charge_negative_rate_exemption(my_predbat)
    return failed
