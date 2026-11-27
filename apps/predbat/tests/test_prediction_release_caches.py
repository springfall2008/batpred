# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for Prediction.release_caches, which update_pred calls once the plan search is over.

The caches are a memo of simulations, so dropping them must leave every later answer unchanged: a run after the
release simulates again and has to agree with the result the memo held.
"""

import prediction_kernel
from prediction import Prediction
from const import EXPORT_MODE_IDLE
from utils import pack_export_limit
from tests.test_infra import reset_inverter, reset_rates


def run_prediction_release_caches_tests(my_predbat):
    """Run the release_caches tests"""
    failed = False
    failed |= test_release_empties_the_caches(my_predbat)
    failed |= test_results_after_release_match_the_cached_ones(my_predbat)
    failed |= test_release_empties_the_export_limit_buffers(my_predbat)
    failed |= test_release_on_a_prediction_without_base(my_predbat)
    return failed


def build_prediction(my_predbat):
    """A Prediction over a day with real rates and a flat PV and load profile"""
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    reset_rates(my_predbat, 10.0, 5.0)
    pv_step = {minute: 0.05 for minute in range(0, my_predbat.forecast_minutes, 5)}
    load_step = {minute: 0.1 for minute in range(0, my_predbat.forecast_minutes, 5)}
    return Prediction(my_predbat, pv_step, pv_step, load_step, load_step)


def run_cached(my_predbat, prediction):
    """One cached prediction run with a charge and an export window"""
    charge_window = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": 10.0}]
    export_window = [{"start": my_predbat.minutes_now + 180, "end": my_predbat.minutes_now + 240, "average": 15.0}]
    return prediction.run_prediction([my_predbat.soc_max], charge_window, export_window, [pack_export_limit(EXPORT_MODE_IDLE)], False, my_predbat.forecast_minutes, save=None, cache=True)


def test_release_empties_the_caches(my_predbat):
    """After a cached run the memo holds an entry; release_caches leaves every memo empty"""
    print("**** test_release_empties_the_caches ****")
    failed = False
    prediction = build_prediction(my_predbat)
    run_cached(my_predbat, prediction)
    prediction.prediction_cache_export_end["key"] = [1.0]
    prediction.trial_memo["key"] = ("inputs", "prepared")
    if not prediction.prediction_cache:
        print("ERROR: a cached run left prediction_cache empty, so this test cannot see a release")
        failed = True
    prediction.release_caches()
    if prediction.prediction_cache or prediction.prediction_cache_export_end or prediction.trial_memo:
        print("ERROR: release_caches left an entry behind: cache {} export_end {} trial_memo {}".format(len(prediction.prediction_cache), len(prediction.prediction_cache_export_end), len(prediction.trial_memo)))
        failed = True
    if not failed:
        print("PASS")
    return failed


def test_results_after_release_match_the_cached_ones(my_predbat):
    """A run after the release simulates again and gives the same answer the memo held"""
    print("**** test_results_after_release_match_the_cached_ones ****")
    failed = False
    prediction = build_prediction(my_predbat)
    first = run_cached(my_predbat, prediction)
    memo_hit = run_cached(my_predbat, prediction)
    prediction.release_caches()
    rerun = run_cached(my_predbat, prediction)
    if memo_hit[:11] != first[:11]:
        print("ERROR: the memo hit differs from the run that filled it: {} vs {}".format(memo_hit[:11], first[:11]))
        failed = True
    if rerun[:11] != first[:11]:
        print("ERROR: the run after release_caches differs from the cached result: {} vs {}".format(rerun[:11], first[:11]))
        failed = True
    if not prediction.prediction_cache:
        print("ERROR: the run after release_caches did not repopulate the memo")
        failed = True
    if not failed:
        print("PASS")
    return failed


def test_release_empties_the_export_limit_buffers(my_predbat):
    """release_caches also drops the kernel's contents-keyed export-limit buffers, and they are rebuilt on demand"""
    print("**** test_release_empties_the_export_limit_buffers ****")
    failed = False
    limits = [pack_export_limit(EXPORT_MODE_IDLE)]
    first = bytes(prediction_kernel.export_limit_array(limits))
    if not prediction_kernel._EXPORT_LIMIT_BUFFER_CACHE:
        print("ERROR: export_limit_array did not fill the buffer cache, so this test cannot see a release")
        failed = True
    Prediction().release_caches()
    if prediction_kernel._EXPORT_LIMIT_BUFFER_CACHE:
        print("ERROR: release_caches left {} export-limit buffers behind".format(len(prediction_kernel._EXPORT_LIMIT_BUFFER_CACHE)))
        failed = True
    if bytes(prediction_kernel.export_limit_array(limits)) != first:
        print("ERROR: the export-limit buffer rebuilt after release differs from the cached one")
        failed = True
    if not failed:
        print("PASS")
    return failed


def test_release_on_a_prediction_without_base(my_predbat):
    """A Prediction built without a base has no prediction_cache yet; releasing it must not raise"""
    print("**** test_release_on_a_prediction_without_base ****")
    failed = False
    prediction = Prediction()
    try:
        prediction.release_caches()
    except Exception as error:
        print("ERROR: release_caches raised on a Prediction without a base: {}".format(error))
        failed = True
    if getattr(prediction, "prediction_cache", None) != {}:
        print("ERROR: release_caches did not leave an empty prediction_cache")
        failed = True
    if not failed:
        print("PASS")
    return failed
