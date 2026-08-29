# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

import os

from tests import test_infra
from tests.test_infra import reset_rates, reset_inverter, simple_scenario, set_plot_enabled


def _make_prediction(my_predbat):
    """
    Run a trivial passing scenario and hand back its real prediction object to plot

    The Python engine is forced on because predict_metric_best - one of the series plot() draws -
    is only recorded when run_prediction is given a save mode, which simple_scenario suppresses
    for kernel-eligible runs.
    """
    my_predbat.prediction_kernel_enable = False
    _failed, prediction = simple_scenario(
        "plot_fixture",
        my_predbat,
        1.0,
        0,
        assert_final_metric=10.0,
        assert_final_soc=10.0,
        battery_size=10.0,
        battery_soc=10.0,
        end_record=60,
        battery_rate_max_charge=0.0,
        return_prediction_handle=True,
        quiet=True,
    )
    return prediction


def run_plot_tests(my_predbat):
    """
    The failure plot must not block the run unless it was explicitly asked for.

    plot() is called whenever a simple_scenario assertion fails. Its plt.show() opens a window
    and blocks until that window is closed, so on any machine with a display a failing test run
    never terminates - it looks like a hang rather than a test failure. Displaying is therefore
    opt-in via the harness --plot flag; the PNG is still always written either way.
    """
    print("**** Running Plot tests ****")
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)

    failed = False
    prediction = _make_prediction(my_predbat)

    shown = []
    real_show = test_infra.plt.show
    test_infra.plt.show = lambda *args, **kwargs: shown.append(True)
    previous = test_infra.PLOT_ENABLED
    png = "plot_display_is_opt_in.png"

    try:
        # Default: write the PNG, but never block on a window
        set_plot_enabled(False)
        if os.path.exists(png):
            os.remove(png)
        test_infra.plot("plot_display_is_opt_in", prediction)

        if shown:
            print("ERROR: plot() called plt.show() when plotting was disabled - a failing test run would hang")
            failed = True
        else:
            print("Test: plot() does not block on a window when plotting is disabled PASSED")

        if not os.path.exists(png):
            print("ERROR: plot() should still write {} when plotting is disabled".format(png))
            failed = True
        else:
            print("Test: plot() still writes the PNG when plotting is disabled PASSED")

        # Opt in via the harness flag and the window comes back
        set_plot_enabled(True)
        test_infra.plot("plot_display_is_opt_in", prediction)

        if not shown:
            print("ERROR: plot() did not call plt.show() after --plot enabled it")
            failed = True
        else:
            print("Test: plot() shows the window when plotting is enabled PASSED")
    finally:
        test_infra.plt.show = real_show
        set_plot_enabled(previous)
        if os.path.exists(png):
            os.remove(png)

    return failed
