# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the series switch on run_prediction and its use by run_prediction_metric.

A prediction returns a per-step SoC series as well as its totals. run_prediction_metric runs up to
three scenarios for one plan and keeps only the nominal run's series: it reads the export window-end
SoCs from it, and it is what the caller is left holding in self.predict_soc. The PV10 and PV90 series
are overwritten on the next line, so those two runs are made with series=False and the kernel does
not produce one.

These tests pin the three things that makes safe, on the Python engine and on the kernel:

- series=False changes nothing in a prediction's result except the series itself
- run_prediction_metric returns what it did when every run produced a series, and still leaves the
  nominal run's series and the export window-end SoCs behind
- only the PV10 and PV90 runs go without a series; the nominal run and a nominal_only metric keep it
"""

import random

import prediction
from const import EXPORT_MODE_TARGET, PREDICT_STEP, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90
from prediction import Prediction
from prediction_batch import export_end_soc_from_series, prediction_cache_key
from tests.test_infra import update_rates_export
from tests.test_kernel_parity import kernel_available
from tests.test_optimise_swap_charge import setup_swap_charge
from utils import pack_export_limit

SCENARIO_NAMES = {PV_SCENARIO_NOMINAL: "nominal", PV_SCENARIO_PV10: "pv10", PV_SCENARIO_PV90: "pv90"}


def setup_metric_scenario(my_predbat, seed, kernel, pv90_weight):
    """Build a plan with charge windows, solar and a target export, returning it as (limits, windows, export windows, export limits).

    The solar has a PV10/PV90 spread so the three scenarios differ, and the target export comes after
    the charge windows so the PV10 run takes its export floor from the nominal run's window-end SoC.
    """
    rng = random.Random(seed)
    minutes_now = my_predbat.minutes_now
    soc_max = 10.0
    n_windows = 8

    charge_window_best = [{"start": minutes_now + n * 30, "end": minutes_now + (n + 1) * 30, "average": rng.choice([4.0, 7.5, 11.0, 16.0, 24.0])} for n in range(n_windows)]
    charge_limit_best = [rng.choice([0.0, 0.0, 4.0, soc_max]) for _ in range(n_windows)]
    setup_swap_charge(my_predbat, charge_window_best, charge_limit_best, battery_size=soc_max, charge_rate=6.0)

    export_start = minutes_now + n_windows * 30
    export_window_best = [{"start": export_start, "end": export_start + 60, "average": 28.0}, {"start": export_start + 120, "end": export_start + 150, "average": 22.0}]
    update_rates_export(my_predbat, export_window_best)
    export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 30), pack_export_limit(EXPORT_MODE_TARGET, 10)]
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = export_limits_best

    pv_step = {}
    pv10_step = {}
    pv90_step = {}
    load_step = {}
    load10_step = {}
    load90_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_kw = 3.0 * max(0.0, 1.0 - minute / 180.0)
        load_kw = round(rng.uniform(0.4, 1.2), 3)
        pv_step[minute] = pv_kw / 12.0
        pv10_step[minute] = pv_kw * 0.3 / 12.0
        pv90_step[minute] = pv_kw * 1.4 / 12.0
        load_step[minute] = load_kw / 12.0
        load10_step[minute] = load_kw * 1.1 / 12.0
        load90_step[minute] = load_kw * 0.95 / 12.0
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load10_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv10_step

    # The Prediction reads the kernel and debug switches when it is built. Debug stays off: a debug
    # run is never given to the kernel.
    my_predbat.pv_metric90_weight = pv90_weight
    my_predbat.prediction_kernel_enable = kernel
    my_predbat.debug_enable = False
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step, pv90_step, load90_step)
    return list(charge_limit_best), charge_window_best, export_window_best, export_limits_best


def metric_with_every_series(my_predbat, plan, end_record):
    """run_prediction_metric as it was before the series switch: all three runs produce their series.

    Returns the metric tuple, the nominal run's series and the nominal run's other per-run state.
    """
    charge_limit, charge_window, export_window, export_limits = plan
    (cost, import_kwh_battery, import_kwh_house, export_kwh, _, soc, _, battery_cycle, metric_keep, final_iboost, final_carbon_g) = my_predbat.run_prediction(
        charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record=end_record
    )
    nominal_series = my_predbat.predict_soc
    nominal_state = (my_predbat.car_charging_soc_next, my_predbat.iboost_next, my_predbat.iboost_running, my_predbat.iboost_running_solar, my_predbat.iboost_running_full)
    soc90 = None
    cost90 = None
    final_iboost90 = 0.0
    if my_predbat.pv_metric90_weight > 0:
        (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = my_predbat.run_prediction(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV90, end_record=end_record)
    (cost10, _, _, _, _, soc10, _, _, _, final_iboost10, _) = my_predbat.run_prediction(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, end_record=end_record)
    metric, battery_value = my_predbat.compute_metric(
        my_predbat.end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90
    )
    return (metric, battery_value, cost, metric_keep, battery_cycle, final_carbon_g, import_kwh_battery + import_kwh_house, export_kwh), nominal_series, nominal_state


def test_series_off_changes_only_the_series(my_predbat, kernel_modes):
    """series=False empties the series and leaves the other sixteen fields of the result as they were"""
    print("**** test_series_off_changes_only_the_series ****")
    failed = False
    checked = 0
    for kernel in kernel_modes:
        for seed in range(4):
            plan = setup_metric_scenario(my_predbat, seed, kernel, pv90_weight=0.15)
            end_record = my_predbat.end_record
            for pv_scenario in (PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90):
                name = "kernel={} seed={} {}".format(kernel, seed, SCENARIO_NAMES[pv_scenario])
                full = my_predbat.prediction.run_prediction(*plan, pv_scenario, end_record)
                bare = my_predbat.prediction.run_prediction(*plan, pv_scenario, end_record, series=False)
                if len(full) != 17 or len(bare) != 17:
                    print("ERROR: {}: expected 17 fields, got {} with the series and {} without".format(name, len(full), len(bare)))
                    failed = True
                    continue
                if len(full[11]) < 12:
                    print("ERROR: {}: expected a SoC series by default, got {} points".format(name, len(full[11])))
                    failed = True
                if bare[11] != {}:
                    print("ERROR: {}: expected an empty series with series=False, got {} points".format(name, len(bare[11])))
                    failed = True
                if bare[:11] != full[:11] or bare[12:] != full[12:]:
                    print("ERROR: {}: series=False changed the result\n  with    {}\n  without {}".format(name, full[:11] + full[12:], bare[:11] + bare[12:]))
                    failed = True
                checked += 1

    if checked != len(kernel_modes) * 4 * 3:
        print("ERROR: expected to check {} predictions but checked {}".format(len(kernel_modes) * 4 * 3, checked))
        failed = True
    if not failed:
        print("PASS ({} predictions)".format(checked))
    return failed


def test_metric_matches_every_series_reference(my_predbat, kernel_modes):
    """run_prediction_metric gives the metric it gave when all three runs produced a series, and leaves the nominal run's behind"""
    print("**** test_metric_matches_every_series_reference ****")
    failed = False
    checked = 0
    metrics_seen = set()
    for kernel in kernel_modes:
        for pv90_weight in (0.0, 0.15):
            for seed in range(6):
                name = "kernel={} pv90_weight={} seed={}".format(kernel, pv90_weight, seed)
                plan = setup_metric_scenario(my_predbat, seed, kernel, pv90_weight)
                end_record = my_predbat.end_record
                expect, nominal_series, nominal_state = metric_with_every_series(my_predbat, plan, end_record)

                # A fresh Prediction so nothing is carried over from the reference runs
                plan = setup_metric_scenario(my_predbat, seed, kernel, pv90_weight)
                got = my_predbat.run_prediction_metric(*plan, end_record=end_record)
                checked += 1
                metrics_seen.add(got[0])

                if got != expect:
                    print("ERROR: {}: metric changed\n  expected {}\n  got      {}".format(name, expect, got))
                    failed = True
                if len(nominal_series) < 12 or my_predbat.predict_soc != nominal_series:
                    print("ERROR: {}: expected the nominal run's {} point series to be left in predict_soc, got {} points".format(name, len(nominal_series), len(my_predbat.predict_soc)))
                    failed = True
                state = (my_predbat.car_charging_soc_next, my_predbat.iboost_next, my_predbat.iboost_running, my_predbat.iboost_running_solar, my_predbat.iboost_running_full)
                if state != nominal_state:
                    print("ERROR: {}: expected the nominal run's car and iBoost state {} to be left in place, got {}".format(name, nominal_state, state))
                    failed = True

                charge_limit, charge_window, export_window, export_limits = plan
                key = prediction_cache_key(charge_limit, charge_window, export_limits, export_window, PV_SCENARIO_NOMINAL, end_record, PREDICT_STEP)
                expect_end_socs = export_end_soc_from_series(nominal_series, export_window, my_predbat.minutes_now, export_limits)
                got_end_socs = my_predbat.prediction.prediction_cache_export_end.get(key)
                if got_end_socs != expect_end_socs:
                    print("ERROR: {}: expected the nominal window-end SoCs {} to be recorded for the pv10 floor, got {}".format(name, expect_end_socs, got_end_socs))
                    failed = True

    # Guard against a scenario so flat that every plan scores the same and the comparison proves nothing
    if len(metrics_seen) < 4:
        print("ERROR: expected the scenarios to give different metrics, got {}".format(sorted(metrics_seen)))
        failed = True
    if not failed:
        print("PASS ({} metrics, {} distinct)".format(checked, len(metrics_seen)))
    return failed


def record_series_requests(my_predbat):
    """Wrap this Prediction's run_prediction to record (scenario, series asked for, points returned) per call"""
    calls = []
    real_run = my_predbat.prediction.run_prediction

    def spy(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, *args, **kwargs):
        """Record what was asked for and what came back, then return it unchanged"""
        result = real_run(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, *args, **kwargs)
        calls.append((int(pv_scenario), kwargs.get("series", True), len(result[11])))
        return result

    my_predbat.prediction.run_prediction = spy
    return calls


def test_metric_asks_only_nominal_for_the_series(my_predbat, kernel_modes):
    """The nominal run is made with its series, the pv10 and pv90 runs without, and nominal_only keeps it"""
    print("**** test_metric_asks_only_nominal_for_the_series ****")
    failed = False
    for kernel in kernel_modes:
        for pv90_weight, expect_order in ((0.0, [PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10]), (0.15, [PV_SCENARIO_NOMINAL, PV_SCENARIO_PV90, PV_SCENARIO_PV10])):
            name = "kernel={} pv90_weight={}".format(kernel, pv90_weight)
            plan = setup_metric_scenario(my_predbat, 1, kernel, pv90_weight)
            calls = record_series_requests(my_predbat)
            my_predbat.run_prediction_metric(*plan, end_record=my_predbat.end_record)

            # One run per scenario: the pv10 floor comes from the recorded window-end SoCs, not a second nominal run
            if [call[0] for call in calls] != expect_order:
                print("ERROR: {}: expected runs {} but got {}".format(name, expect_order, [call[0] for call in calls]))
                failed = True
            for pv_scenario, series, points in calls:
                want_series = pv_scenario == PV_SCENARIO_NOMINAL
                if series != want_series or (points > 0) != want_series:
                    print("ERROR: {}: {} run should {} a series, asked series={} and got {} points".format(name, SCENARIO_NAMES[pv_scenario], "have" if want_series else "not have", series, points))
                    failed = True

        name = "kernel={} nominal_only".format(kernel)
        plan = setup_metric_scenario(my_predbat, 1, kernel, 0.15)
        calls = record_series_requests(my_predbat)
        my_predbat.run_prediction_metric(*plan, end_record=my_predbat.end_record, nominal_only=True)
        if len(calls) != 1 or calls[0][0] != PV_SCENARIO_NOMINAL or not calls[0][1] or calls[0][2] < 12:
            print("ERROR: {}: expected one nominal run with its series, got {}".format(name, calls))
            failed = True
        if len(my_predbat.predict_soc) < 12:
            print("ERROR: {}: expected the nominal series to be left in predict_soc, got {} points".format(name, len(my_predbat.predict_soc)))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_kernel_is_not_asked_for_unwanted_series(my_predbat):
    """With the kernel on, the three runs of a metric reach the kernel and only the nominal one asks it for a series"""
    print("**** test_kernel_is_not_asked_for_unwanted_series ****")
    failed = False
    plan = setup_metric_scenario(my_predbat, 2, True, 0.15)
    kernel_calls = []
    real_kernel_run = prediction.run_prediction_kernel

    def kernel_spy(pred, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache, **kwargs):
        """Record the scenario and the series flag each kernel run was given"""
        result = real_kernel_run(pred, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache, **kwargs)
        kernel_calls.append((int(pv_scenario), kwargs.get("series", True), result is not None))
        return result

    prediction.run_prediction_kernel = kernel_spy
    try:
        my_predbat.run_prediction_metric(*plan, end_record=my_predbat.end_record)
    finally:
        prediction.run_prediction_kernel = real_kernel_run

    expect = [(PV_SCENARIO_NOMINAL, True, True), (PV_SCENARIO_PV90, False, True), (PV_SCENARIO_PV10, False, True)]
    if kernel_calls != expect:
        print("ERROR: expected kernel runs (scenario, series, ran) {} but got {}".format(expect, kernel_calls))
        failed = True

    if not failed:
        print("PASS")
    return failed


def run_metric_series_tests(my_predbat):
    """Run the series switch tests on the Python engine and, where it is available, the kernel, returns True on failure."""
    available, required_failure = kernel_available()
    if required_failure:
        return True
    kernel_modes = [False, True] if available else [False]

    saved = (my_predbat.pv_metric90_weight, getattr(my_predbat, "prediction_kernel_enable", False), my_predbat.debug_enable)
    failed = False
    try:
        failed |= test_series_off_changes_only_the_series(my_predbat, kernel_modes)
        failed |= test_metric_matches_every_series_reference(my_predbat, kernel_modes)
        failed |= test_metric_asks_only_nominal_for_the_series(my_predbat, kernel_modes)
        if available:
            failed |= test_kernel_is_not_asked_for_unwanted_series(my_predbat)
        else:
            print("WARNING: Prediction kernel not available - kernel series test SKIPPED")
    finally:
        my_predbat.pv_metric90_weight, my_predbat.prediction_kernel_enable, my_predbat.debug_enable = saved
    return failed
