# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the pairwise charge-window swap optimisation pass.

optimise_swap_charge exchanges the charge limits of two charge windows on the same day and
keeps the swap only when it strictly lowers the overall metric. This escapes a local optimum
that single-window coordinate descent cannot reach, where the same charge is better placed in
a different (equal or similar priced) window.
"""

from tests.test_infra import reset_rates, reset_inverter, update_rates_import
from prediction import Prediction


def setup_swap_charge(
    my_predbat,
    charge_window_best,
    charge_limit_best,
    rate_import=10.0,
    rate_export=15.0,
    battery_size=10.0,
    battery_soc=0.5,
    calculate_best_charge=True,
    charge_rate=10.0,
):
    """Configure my_predbat for a charge-swap test and return the starting metric."""
    end_record = my_predbat.forecast_minutes
    my_predbat.end_record = end_record
    my_predbat.calculate_best_charge = calculate_best_charge
    my_predbat.calculate_best_export = True
    my_predbat.calculate_export_oncharge = True
    my_predbat.set_charge_freeze = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.reserve = 0.5
    my_predbat.manual_all_times = set()
    my_predbat.iboost_enable = False
    my_predbat.iboost_plan = []
    my_predbat.iboost_on_export = False
    my_predbat.car_charging_from_battery = True
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots = [[]]

    # A generous battery charge rate so a 30 minute window can move a meaningful amount of energy
    my_predbat.battery_rate_max_charge = charge_rate / 60.0
    my_predbat.battery_rate_max_charge_dc = charge_rate / 60.0

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_import(my_predbat, charge_window_best)

    # No solar and a small flat load
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 0.2 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    my_predbat.charge_window_best = charge_window_best
    my_predbat.charge_limit_best = list(charge_limit_best)
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []

    best_metric, _, best_cost, _, _, _, _, _ = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=end_record)
    return best_metric, best_cost


def run_optimise_swap_charge_tests(my_predbat):
    """Run the pairwise charge-window swap optimisation tests and return True on failure."""
    print("**** Running Optimise swap charge tests ****")
    reset_inverter(my_predbat)
    failed = False

    soc_max = 10.0

    # ---------------------------------------------------------------------------------------------
    # Beneficial swap: the plan charges the battery in the later (expensive) window while the
    # earlier (cheap) window is idle. Swapping the two limits charges the same amount in the cheap
    # window instead, which strictly lowers the cost and metric with an identical final SoC.
    # ---------------------------------------------------------------------------------------------
    charge_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 5.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 30.0},
    ]
    start_metric, start_cost = setup_swap_charge(my_predbat, charge_window_best, charge_limit_best=[0.0, soc_max])
    my_predbat.optimise_swap_charge(len(charge_window_best))
    end_metric, _, end_cost, _, _, _, _, _ = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)

    if not (my_predbat.charge_limit_best[0] == soc_max and my_predbat.charge_limit_best[1] == 0.0):
        print("ERROR: beneficial swap expected charge to move to window 0, got limits {}".format(my_predbat.charge_limit_best))
        failed = True
    if not (end_metric < start_metric):
        print("ERROR: beneficial swap expected metric to improve, start {} end {}".format(start_metric, end_metric))
        failed = True
    if not (end_cost < start_cost):
        print("ERROR: beneficial swap expected cost to improve, start {} end {}".format(start_cost, end_cost))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Safety invariant: the pass must never make the plan worse. Starting from an already-cheap
    # plan (charge in the cheap early window) there is no improving swap, so the plan is unchanged
    # and the metric does not increase.
    # ---------------------------------------------------------------------------------------------
    start_metric, _ = setup_swap_charge(my_predbat, charge_window_best, charge_limit_best=[soc_max, 0.0])
    my_predbat.optimise_swap_charge(len(charge_window_best))
    end_metric, _, _, _, _, _, _, _ = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)
    if not (my_predbat.charge_limit_best[0] == soc_max and my_predbat.charge_limit_best[1] == 0.0):
        print("ERROR: already-optimal plan should be unchanged, got limits {}".format(my_predbat.charge_limit_best))
        failed = True
    if end_metric > start_metric + 0.0001:
        print("ERROR: safety invariant violated, metric increased from {} to {}".format(start_metric, end_metric))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # No-op: two equal priced windows with equal limits have nothing to swap.
    # ---------------------------------------------------------------------------------------------
    equal_windows = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 10.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 10.0},
    ]
    setup_swap_charge(my_predbat, equal_windows, charge_limit_best=[soc_max, soc_max])
    my_predbat.optimise_swap_charge(len(equal_windows))
    if my_predbat.charge_limit_best != [soc_max, soc_max]:
        print("ERROR: equal windows should be unchanged, got {}".format(my_predbat.charge_limit_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Guard: with a single charge window there is no pair to swap and the plan is left untouched.
    # ---------------------------------------------------------------------------------------------
    single_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 5.0}]
    setup_swap_charge(my_predbat, single_window, charge_limit_best=[soc_max])
    my_predbat.optimise_swap_charge(len(single_window))
    if my_predbat.charge_limit_best != [soc_max]:
        print("ERROR: single window should be unchanged, got {}".format(my_predbat.charge_limit_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Guard: with charge optimisation disabled the pass returns immediately without changes, even
    # when a beneficial swap would otherwise exist.
    # ---------------------------------------------------------------------------------------------
    setup_swap_charge(my_predbat, charge_window_best, charge_limit_best=[0.0, soc_max], calculate_best_charge=False)
    my_predbat.optimise_swap_charge(len(charge_window_best))
    if my_predbat.charge_limit_best != [0.0, soc_max]:
        print("ERROR: disabled charge optimisation should be unchanged, got {}".format(my_predbat.charge_limit_best))
        failed = True

    if failed:
        print("**** Optimise swap charge tests FAILED ****")
    else:
        print("**** Optimise swap charge tests passed ****")
    return failed
