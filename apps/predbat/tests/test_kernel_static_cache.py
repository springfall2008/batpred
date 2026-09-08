# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the load-independent context cache shared by the marginal cost matrix.

calculate_marginal_costs builds 28 Predictions that differ only in their load forecast, and each
one used to rebuild the whole kernel context - 576 steps of rate, PV, carbon, temperature and car
lookups that are identical every time. create_kernel_context now takes a static_cache the caller
reuses across those builds.

The cache is only sound while the caller's promise holds: everything except the load arrays is the
same. The risk it introduces is precisely the opposite - a load array wrongly treated as static,
so cell two silently answers with cell one's load. These tests build contexts both ways and require
each cached one to match its own independent build, and require two different loads to still give
different answers so a leak cannot hide behind coincidentally equal results.
"""
from prediction import Prediction
from prediction_kernel import create_kernel_context, load_kernel
from tests.test_infra import reset_inverter, reset_rates


def run_kernel_static_cache_tests(my_predbat):
    """Run the kernel static context cache tests"""
    if not load_kernel():
        print("**** kernel_static_cache: kernel unavailable, skipping ****")
        return False
    failed = False
    failed |= test_cached_context_matches_an_independent_build(my_predbat)
    failed |= test_different_loads_still_give_different_answers(my_predbat)
    return failed


def build_environment(my_predbat):
    """Set up a predbat exercising the parts of the static context that vary per step.

    The battery temperature changes over the horizon so the temperature memo has to key on the right
    thing, and iboost runs with a real plan so iboost_plan_load is a non-trivial array rather than
    288 zeroes - otherwise the cache could be reusing it wrongly and every comparison would still
    agree.
    """
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.prediction_kernel_enable = True
    my_predbat.battery_temperature = 20
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    # Real rates, or every prediction costs 0.0 and the two loads compare equal - which would leave
    # the leak test above unable to fail
    reset_rates(my_predbat, 10.0, 5.0)
    # A temperature that changes over the horizon, so a memo keyed on it has to key on the right thing.
    # The curves matter as much as the profile: with no curve configured every temperature yields the
    # same cap, and a memo keyed on the wrong thing entirely would still agree at every step.
    my_predbat.battery_temperature_prediction = {minute: max(20 - minute / (2 * 60.0), -5) for minute in range(0, my_predbat.forecast_minutes, 5)}
    my_predbat.battery_temperature_charge_curve = {20: 1.0, 15: 0.9, 10: 0.7, 5: 0.5, 0: 0.3, -5: 0.2, -10: 0.1}
    my_predbat.battery_temperature_discharge_curve = {20: 1.0, 15: 0.95, 10: 0.85, 5: 0.6, 0: 0.4, -5: 0.25, -10: 0.15}

    # iboost on, with a plan covering part of the horizon, so the cached iboost_plan_load array
    # carries real slot data
    my_predbat.iboost_enable = True
    my_predbat.iboost_solar = False
    my_predbat.iboost_charging = False
    my_predbat.iboost_on_export = False
    my_predbat.iboost_prevent_discharge = False
    my_predbat.iboost_max_energy = 5.0
    my_predbat.iboost_max_power = 2.5 / 60.0
    my_predbat.iboost_min_power = 0.0
    my_predbat.iboost_today = 0.0
    my_predbat.iboost_next = 0.0
    my_predbat.iboost_plan = [
        {"start": my_predbat.minutes_now + 120, "end": my_predbat.minutes_now + 240, "kwh": 3.0},
        {"start": my_predbat.minutes_now + 480, "end": my_predbat.minutes_now + 540, "kwh": 1.5},
    ]

    pv_step = {minute: 0.05 for minute in range(0, my_predbat.forecast_minutes, 5)}
    return pv_step


def make_load(my_predbat, base_load):
    """A flat load profile at base_load kWh per 5 minute step"""
    return {minute: base_load for minute in range(0, my_predbat.forecast_minutes, 5)}


def run_one(my_predbat, pv_step, load_step, static_cache):
    """Build a Prediction for this load and return its prediction result"""
    prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    prediction.kernel_handle = create_kernel_context(prediction, static_cache=static_cache)
    if not prediction.kernel_handle:
        return None
    charge_window = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": 10.0}]
    export_window = [{"start": my_predbat.minutes_now + 180, "end": my_predbat.minutes_now + 240, "average": 15.0}]
    return prediction.run_prediction([my_predbat.soc_max], charge_window, export_window, [100.0], False, my_predbat.forecast_minutes, save=None, cache=False)


def test_cached_context_matches_an_independent_build(my_predbat):
    """A context built from a shared cache matches one built entirely on its own.

    The second cached build is the one that matters: its cache was primed by a different load, so if
    the load were treated as static it would answer with the first load's profile.
    """
    print("**** test_cached_context_matches_an_independent_build ****")
    failed = False
    pv_step = build_environment(my_predbat)
    low = make_load(my_predbat, 0.02)
    high = make_load(my_predbat, 0.30)

    fresh_low = run_one(my_predbat, pv_step, low, None)
    fresh_high = run_one(my_predbat, pv_step, high, None)

    shared = {}
    cached_low = run_one(my_predbat, pv_step, low, shared)
    cached_high = run_one(my_predbat, pv_step, high, shared)

    if fresh_low is None or fresh_high is None or cached_low is None or cached_high is None:
        print("ERROR: kernel context creation failed")
        return True

    if cached_low[:11] != fresh_low[:11]:
        print("ERROR: cached low-load context differs from an independent build")
        print("  cached {}".format(cached_low[:11]))
        print("  fresh  {}".format(fresh_low[:11]))
        failed = True
    if cached_high[:11] != fresh_high[:11]:
        print("ERROR: cached high-load context differs from an independent build - the cache leaked a load")
        print("  cached {}".format(cached_high[:11]))
        print("  fresh  {}".format(fresh_high[:11]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_different_loads_still_give_different_answers(my_predbat):
    """Two loads sharing a cache must not collapse onto one answer.

    Without this the leak test above could pass on a fixture where both loads happen to cost the
    same, which would make it unable to fail.
    """
    print("**** test_different_loads_still_give_different_answers ****")
    failed = False
    pv_step = build_environment(my_predbat)
    shared = {}
    cached_low = run_one(my_predbat, pv_step, make_load(my_predbat, 0.02), shared)
    cached_high = run_one(my_predbat, pv_step, make_load(my_predbat, 0.30), shared)

    if cached_low is None or cached_high is None:
        print("ERROR: kernel context creation failed")
        return True

    if cached_low[0] == cached_high[0]:
        print("ERROR: a 15x load difference produced the same metric ({}), so a leaked load would be invisible here".format(cached_low[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed
