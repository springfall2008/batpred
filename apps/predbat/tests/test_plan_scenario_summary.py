# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from const import EXPORT_MODE_FREEZE, EXPORT_MODE_TARGET
from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates, update_rates_import
from utils import calc_percent_limit, pack_export_limit


def run_test_plan_scenario_summary(my_predbat):
    """
    Regression test for GH export-limit refactor: scenario_summary_state() (the debug "STATE:"
    log line) compared an export limit tuple directly against soc_percent_max with `>=`, a leftover
    bare-number comparison the accessor sweep missed. It never raised in the quick suite because
    the call site is buried behind self.debug_enable's verbose logging, only executing once a real
    plan produced an active FREEZE/TARGET export window below the current SoC - which is exactly
    what happened on first deploy, surfacing as `TypeError: '>=' not supported between instances of
    'list' and 'float'` (or a tuple, depending on how the limit had been round-tripped). This test
    calls scenario_summary_state() directly with a tuple-form export limit so a future bare
    comparison on this path fails a quick-suite run instead of a live deploy.
    """
    print("**** Running plan scenario_summary_state tests ****")
    failed = False

    # Imported here rather than at module scope: unit_test imports this module, so a top level
    # import would be circular. This regression mutates the full planning fixture, so run it on a
    # throwaway PredBat instance rather than leaking scenario state into the shared suite fixture.
    from unit_test import create_predbat

    my_predbat = create_predbat()

    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 48 * 60
    my_predbat.debug_enable = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.num_inverters = 1
    my_predbat.reserve = 0.5
    my_predbat.reserve_percent = calc_percent_limit(my_predbat.reserve, my_predbat.soc_max)
    my_predbat.set_charge_freeze = True

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0
        load_step[minute] = 0.5 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    baseline_charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]
    reset_rates(my_predbat, 10.0, 5.0)
    update_rates_import(my_predbat, baseline_charge_window)

    # Populate predict_soc_best/record_time/etc with a real (throwaway) prediction run
    my_predbat.run_prediction([0], baseline_charge_window, [], [], False, end_record=my_predbat.end_record, save="best")

    minutes_now = my_predbat.minutes_now
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []

    # SoC well below the export target, so soc_percent_max < the target's sort key and
    # scenario_summary_state must not raise comparing the tuple-form limit against it.
    my_predbat.predict_soc_best = {minute: 2.0 for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes + 5, 5)}

    record_time = my_predbat.prediction.record_time

    for label, export_limit in (
        ("target export window", pack_export_limit(EXPORT_MODE_TARGET, 50)),
        ("freeze export window", pack_export_limit(EXPORT_MODE_FREEZE)),
    ):
        my_predbat.export_window_best = [{"start": minutes_now, "end": minutes_now + 30, "average": 10.0}]
        my_predbat.export_limits_best = [export_limit]
        try:
            my_predbat.scenario_summary_state(record_time)
        except TypeError as e:
            print("ERROR: scenario_summary_state raised for {}: {}".format(label, e))
            failed = True

    print("**** Plan scenario_summary_state tests passed: {} ****".format(not failed))
    return failed
