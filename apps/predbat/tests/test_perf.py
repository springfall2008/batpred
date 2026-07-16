# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import math
import time
from tests.test_infra import reset_rates, reset_inverter, simple_scenario
from tests.test_kernel_parity import kernel_available

PERF_ITERATIONS = 200


def safe_ratio(numerator, denominator):
    """Divide, returning infinity instead of raising when the elapsed time rounds to 0 (very fast runs or a coarse clock)"""
    return numerator / denominator if denominator > 0 else math.inf


def time_perf_scenario(my_predbat, import_rate):
    """Time the standard perf scenario for PERF_ITERATIONS runs, returns (failed, seconds)"""
    failed, prediction_handle = simple_scenario(
        "load_bat_dc_pv2",
        my_predbat,
        4,
        4,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=50 + 24,
        with_battery=True,
        battery_soc=50.0,
        inverter_loss=0.8,
        hybrid=True,
        quiet=True,
        save="none",
        return_prediction_handle=True,
        ignore_failed=True,
    )

    start_time = time.time()
    for count in range(0, PERF_ITERATIONS):
        failed |= simple_scenario(
            "load_bat_dc_pv2",
            my_predbat,
            4,
            4,
            assert_final_metric=import_rate * 24 * 3.2,
            assert_final_soc=50 + 24,
            with_battery=True,
            battery_soc=50.0,
            inverter_loss=0.8,
            hybrid=True,
            quiet=True,
            save="none",
            prediction_handle=prediction_handle,
            ignore_failed=True,
        )
    end_time = time.time()
    return failed, end_time - start_time


def run_perf_test(my_predbat):
    """Performance test of the prediction engine, comparing the Python engine and the C++ kernel"""
    print("**** Running Performance tests ****")
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    reset_rates(my_predbat, import_rate, export_rate)
    failed = False

    my_predbat.prediction_cache_enable = False

    # Python engine
    my_predbat.prediction_kernel_enable = False
    failed_python, python_time = time_perf_scenario(my_predbat, import_rate)
    failed |= failed_python
    python_rate = safe_ratio(PERF_ITERATIONS, python_time)
    print("Performance test (Python engine) took {} seconds for {} iterations = {} iterations per second".format(round(python_time, 3), PERF_ITERATIONS, round(python_rate, 2)))

    # C++ kernel (skipped when the shared library is not available, unless PREDBAT_KERNEL_REQUIRED=1)
    kernel_loaded, kernel_required_failure = kernel_available()
    if kernel_loaded:
        my_predbat.prediction_kernel_enable = True
        failed_kernel, kernel_time = time_perf_scenario(my_predbat, import_rate)
        failed |= failed_kernel
        kernel_rate = safe_ratio(PERF_ITERATIONS, kernel_time)
        print("Performance test (C++ kernel) took {} seconds for {} iterations = {} iterations per second".format(round(kernel_time, 3), PERF_ITERATIONS, round(kernel_rate, 2)))
        print("C++ kernel speedup: {}x".format(round(safe_ratio(python_time, kernel_time), 1)))
        my_predbat.prediction_kernel_enable = False
    elif kernel_required_failure:
        failed = True
    else:
        print("C++ kernel not available - kernel performance test skipped")

    if failed:
        print("Performance test failed")

    return failed
