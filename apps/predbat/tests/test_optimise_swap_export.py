# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the pairwise export-window swap optimisation pass.

optimise_swap_export moves an export later in the day when doing so costs no more than
metric_min_improvement_swap (default -0.25p, i.e. a small regression is accepted to defer the
export). Holding charge for longer hedges against the forecast being wrong, so a later export of
equal value is always preferred.

The pass can only defer exports that exist when it runs, so these tests also pin its position in
optimise_all_windows: it has to run after every pass that can turn an export on, otherwise the
exports those passes add are never considered for deferral (#4478). The budgeted plan pass in particular
only reaches the near-term windows, so it can only ever add exports at the front - exactly the ones this
pass exists to push back.
"""

from tests.test_infra import reset_rates, reset_inverter, update_rates_export
from prediction import Prediction


def setup_swap_export(
    my_predbat,
    export_window_best,
    export_limits_best,
    rate_import=30.0,
    rate_export=15.0,
    battery_size=10.0,
    battery_soc=10.0,
    export_rate=10.0,
):
    """Configure my_predbat for an export-swap test and return the starting metric and cost."""
    end_record = my_predbat.forecast_minutes
    my_predbat.end_record = end_record
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.calculate_export_oncharge = True
    my_predbat.set_export_freeze = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.reserve = 0.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.manual_all_times = set()
    my_predbat.iboost_enable = False
    my_predbat.iboost_plan = []
    my_predbat.iboost_on_export = False
    my_predbat.car_charging_from_battery = True
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots = [[]]
    my_predbat.metric_min_improvement_swap = -0.25

    # A generous battery export rate so a 30 minute window can move a meaningful amount of energy
    my_predbat.battery_rate_max_discharge = export_rate / 60.0
    my_predbat.battery_rate_max_export = export_rate / 60.0

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_export(my_predbat, export_window_best)

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
    my_predbat.debug_enable = False

    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = list(export_limits_best)

    best_metric, _, best_cost, _, _, _, _, _ = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=end_record)
    return best_metric, best_cost


def _record_pass_order(my_predbat):
    """Stub out every optimisation pass so optimise_all_windows just records the call order.

    Returns (order, restore): order is the list the stubs append to, filled in when
    optimise_all_windows is called, and restore() puts the real methods back. my_predbat is shared
    across the whole test run, so leaving the stubs in place would break every later test.
    """
    order = []
    stubbed = (
        "optimise_levels_pass",
        "optimise_detailed_pass",
        "optimise_plan_pass",
        "optimise_solar",
        "optimise_swap_export",
        "optimise_swap_charge",
        "optimise_charge_windows_reset",
        "optimise_charge_windows_manual",
        "plan_write_debug",
    )
    saved = {name: my_predbat.__dict__.get(name, None) for name in stubbed}

    def restore():
        """Remove the stubs so the class methods are visible again."""
        for name, value in saved.items():
            if value is None:
                my_predbat.__dict__.pop(name, None)
            else:
                my_predbat.__dict__[name] = value

    def stub(name, result):
        """Build a recording stub returning a fixed result."""

        def _call(*args, **kwargs):
            """Record the call and return the canned result."""
            order.append(name)
            return result

        return _call

    zeros12 = tuple([0.0] * 12)
    zeros8 = tuple([0.0] * 8)
    zeros6 = tuple([0.0] * 6)

    my_predbat.optimise_levels_pass = stub("levels", zeros12)
    my_predbat.optimise_detailed_pass = stub("detailed", zeros8)
    my_predbat.optimise_plan_pass = stub("plan_pass", zeros6)
    my_predbat.optimise_solar = stub("solar", zeros6)
    my_predbat.optimise_swap_export = stub("swap_export", None)
    my_predbat.optimise_swap_charge = stub("swap_charge", None)

    # Passes that are not part of the ordering under test but would otherwise touch the plan
    my_predbat.optimise_charge_windows_reset = lambda reset_all: None
    my_predbat.optimise_charge_windows_manual = lambda: None
    my_predbat.plan_write_debug = lambda *args, **kwargs: None

    return order, restore


def run_optimise_swap_export_tests(my_predbat):
    """Run the pairwise export-window swap optimisation tests and return True on failure."""
    print("**** Running Optimise swap export tests ****")
    reset_inverter(my_predbat)
    failed = False

    # ---------------------------------------------------------------------------------------------
    # Beneficial swap: the plan exports in the first window while an identically priced later window
    # sits idle. Deferring the export holds the charge for longer at no extra cost, so the pass must
    # move it to the later window.
    # ---------------------------------------------------------------------------------------------
    export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[0.0, 100.0])
    my_predbat.optimise_swap_export(0, len(export_window_best))
    if not (my_predbat.export_limits_best[0] == 100.0 and my_predbat.export_limits_best[1] == 0.0):
        print("ERROR: equal priced windows should defer the export to the later window, got limits {}".format(my_predbat.export_limits_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Safety invariant: an export already in the latest window has nowhere better to go and must be
    # left alone.
    # ---------------------------------------------------------------------------------------------
    export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[100.0, 0.0])
    my_predbat.optimise_swap_export(0, len(export_window_best))
    if not (my_predbat.export_limits_best[0] == 100.0 and my_predbat.export_limits_best[1] == 0.0):
        print("ERROR: an export already in the last window should be unchanged, got limits {}".format(my_predbat.export_limits_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Guard: with export optimisation disabled the pass returns immediately without changes.
    # ---------------------------------------------------------------------------------------------
    export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[0.0, 100.0])
    my_predbat.calculate_best_export = False
    my_predbat.optimise_swap_export(0, len(export_window_best))
    if my_predbat.export_limits_best != [0.0, 100.0]:
        print("ERROR: disabled export optimisation should be unchanged, got {}".format(my_predbat.export_limits_best))
        failed = True
    my_predbat.calculate_best_export = True

    # ---------------------------------------------------------------------------------------------
    # Ordering regression (#4478): the export swap must run after every pass that can enable an
    # export, otherwise the exports the plan pass / optimise_solar add are never
    # considered for deferral and stay pinned at the front of the plan.
    # ---------------------------------------------------------------------------------------------
    for second_pass in (False, True):
        my_predbat.calculate_second_pass = second_pass
        my_predbat.export_more_solar = True
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        order, restore = _record_pass_order(my_predbat)
        try:
            my_predbat.optimise_all_windows(0.0, 0.0)
        finally:
            restore()

        adding_pass = "plan_pass"
        if "swap_export" not in order:
            print("ERROR: optimise_swap_export was not called at all, order {}".format(order))
            failed = True
        elif order.count("swap_export") != 1:
            print("ERROR: optimise_swap_export should be called exactly once, order {}".format(order))
            failed = True
        else:
            for earlier in (adding_pass, "solar"):
                if earlier in order and order.index("swap_export") < order.index(earlier):
                    print("ERROR: optimise_swap_export runs before {} so exports it adds are never deferred, order {}".format(earlier, order))
                    failed = True

    if failed:
        print("**** Optimise swap export tests FAILED ****")
    else:
        print("**** Optimise swap export tests passed ****")
    return failed
