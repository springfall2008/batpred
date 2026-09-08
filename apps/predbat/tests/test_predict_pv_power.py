# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from prediction import Prediction
from tests.test_infra import reset_inverter


def _run_flat_pv(my_predbat, plan_interval_minutes, flat_pv_kw):
    """
    Run a real run_prediction(save="best") with a known, flat PV forecast and return the
    resulting Prediction object so predict_pv_power can be inspected.
    """
    my_predbat.plan_interval_minutes = plan_interval_minutes
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes

    per_minute_kwh = flat_pv_kw / 60.0
    horizon = my_predbat.forecast_minutes + my_predbat.minutes_now + 10
    my_predbat.pv_forecast_minute = {m: per_minute_kwh for m in range(0, horizon)}
    my_predbat.pv_forecast_minute10 = dict(my_predbat.pv_forecast_minute)
    my_predbat.load_minutes_step = {m: 0.0 for m in range(0, horizon)}
    my_predbat.load_minutes_step10 = dict(my_predbat.load_minutes_step)

    pv_step = {m: per_minute_kwh * 5 for m in range(0, horizon, 5)}
    load_step = {m: 0.0 for m in range(0, horizon, 5)}
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    my_predbat.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")
    return my_predbat.prediction


def test_predict_pv_power_matches_input_regardless_of_plan_interval(my_predbat):
    """
    Regression test for issue #4361: predict_pv_power (which feeds pv_power_best, the source of
    the "PV Power (Predicted)" trace on the LoadMLPower chart) must report the same instantaneous
    kW as the underlying flat PV forecast, regardless of plan_interval_minutes.

    Root cause: the formula converts two summed PREDICT_STEP (5-min) energy chunks into an
    instantaneous kW reading via a 60/(2*step) constant - PR #2865 (2025-11-07) accidentally
    replaced that constant with self.plan_interval_minutes, which is unrelated to how many raw
    5-minute steps are being summed. That made the reported PV power scale with
    plan_interval_minutes/30 - correct only for the 30-minute default, and exactly half for
    anyone on 15-minute plan intervals (as in #4361), a third for 10-minute, etc.
    """
    print("**** Running predict_pv_power plan-interval-independence test ****")
    failed = False

    # my_predbat is a shared fixture reused by every test module in the suite - save everything
    # this test mutates and restore it afterwards so later tests (e.g. model_kernel, which assumes
    # the 30-minute default) aren't left running against a stale plan_interval_minutes. Some of
    # these (load_minutes_step*, prediction) aren't set until a real update_pred() cycle or
    # calculate_plan() runs, so on a fresh fixture they may not exist as attributes at all -
    # restoring them to None in that case would still leave the fixture in a worse state than
    # before (an attribute that exists but is None, instead of not existing), so track presence
    # and delete rather than assign None when the attribute was absent to begin with.
    attrs_to_restore = ["plan_interval_minutes", "forecast_minutes", "end_record", "pv_forecast_minute", "pv_forecast_minute10", "load_minutes_step", "load_minutes_step10", "prediction"]
    had_attr = {name: hasattr(my_predbat, name) for name in attrs_to_restore}
    original_value = {name: getattr(my_predbat, name, None) for name in attrs_to_restore if had_attr[name]}

    try:
        flat_pv_kw = 4.0
        for plan_interval_minutes in [30, 15, 10]:
            pred = _run_flat_pv(my_predbat, plan_interval_minutes, flat_pv_kw)
            values = list(pred.predict_pv_power.values())
            # Skip the first/last couple of samples in case of edge alignment effects, check a mid-horizon one
            sample = values[len(values) // 2]
            if abs(sample - flat_pv_kw) > 0.01:
                print("ERROR: plan_interval_minutes={} flat PV input={}kW but predict_pv_power sample={}kW".format(plan_interval_minutes, flat_pv_kw, sample))
                failed = True
            else:
                print("  plan_interval_minutes={}: predict_pv_power={}kW (expected {}kW) - OK".format(plan_interval_minutes, sample, flat_pv_kw))
    finally:
        for name in attrs_to_restore:
            if had_attr[name]:
                setattr(my_predbat, name, original_value[name])
            else:
                try:
                    delattr(my_predbat, name)
                except AttributeError:
                    pass

    if failed:
        print("Test: predict_pv_power_matches_input_regardless_of_plan_interval FAILED")
    else:
        print("Test: predict_pv_power_matches_input_regardless_of_plan_interval PASSED")

    return failed


def run_predict_pv_power_tests(my_predbat):
    """Run all predict_pv_power regression tests."""
    failed = False
    failed |= test_predict_pv_power_matches_input_regardless_of_plan_interval(my_predbat)
    return failed
