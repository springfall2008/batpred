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

The pass prices all of a target's partners as one prediction batch, so they run in the C++ kernel
across the configured threads. The batch tests below pin what that relies on: that a batched metric
is the metric run_prediction_metric returns, that the pass ends on the plan a one-at-a-time search
ends on, and that the trials do reach the kernel as a batch with the configured thread count.
"""

import random

import prediction_batch
from const import EXPORT_MODE_TARGET
from prediction import Prediction
from tests.test_infra import reset_rates, reset_inverter, update_rates_import, update_rates_export, clear_manual_times
from tests.test_kernel_parity import kernel_available
from utils import pack_export_limit


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
    clear_manual_times(my_predbat)
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

    failed |= run_swap_charge_batch_tests(my_predbat)

    if failed:
        print("**** Optimise swap charge tests FAILED ****")
    else:
        print("**** Optimise swap charge tests passed ****")
    return failed


def setup_swap_scenario(my_predbat, seed, kernel, pv90_weight=0.0, n_windows=14):
    """Build an afternoon of half-hour charge windows with seeded prices and limits, returning the starting limits.

    Unlike setup_swap_charge's two-window plans this one has enough windows, at enough different
    prices, for a target to have many partners and for some swaps to be worth keeping. It also has
    solar with a PV10/PV90 spread and a target export after the charge windows, so all three
    scenarios differ and the PV10 run takes its export floor from the nominal one.
    """
    rng = random.Random(seed)
    minutes_now = my_predbat.minutes_now
    soc_max = 10.0

    charge_window_best = [{"start": minutes_now + n * 30, "end": minutes_now + (n + 1) * 30, "average": rng.choice([4.0, 7.5, 11.0, 16.0, 24.0, 31.0])} for n in range(n_windows)]
    charge_limit_best = [rng.choice([0.0, 0.0, 0.0, 4.0, soc_max]) for _ in range(n_windows)]
    setup_swap_charge(my_predbat, charge_window_best, charge_limit_best, battery_size=soc_max, charge_rate=6.0)

    export_start = minutes_now + n_windows * 30
    export_window_best = [{"start": export_start, "end": export_start + 60, "average": 28.0}]
    update_rates_export(my_predbat, export_window_best)
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 30)]

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
    return list(charge_limit_best)


def swap_charge_one_at_a_time(my_predbat, record_charge_windows):
    """The search optimise_swap_charge ran before it batched its trials, returning (swaps kept, of those not the first partner tried).

    Each swap is made in place, priced with run_prediction_metric and undone if it is not kept. This
    is the reference the batched pass is checked against, so it is a copy of that loop and not a
    call into anything the pass now shares.
    """
    min_improvement_swap = 0.1
    kept = 0
    kept_later_partner = 0
    swapped_target = {}
    swapped = True
    while swapped:
        selected_metric = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[0]
        swapped = False
        for window_n_target in range(record_charge_windows - 1, 0, -1):
            window_start_target = my_predbat.charge_window_best[window_n_target]["start"]
            charge_limit_target = my_predbat.charge_limit_best[window_n_target]
            if window_start_target in my_predbat.manual_all_times or swapped_target.get(window_n_target, False) or not my_predbat.allow_this_charge_window(window_n_target):
                continue
            tried = 0
            for window_n in range(max(window_n_target - 32, 0), window_n_target, 1):
                window_start = my_predbat.charge_window_best[window_n]["start"]
                charge_limit = my_predbat.charge_limit_best[window_n]
                if window_start in my_predbat.manual_all_times or window_start // 1440 != window_start_target // 1440 or charge_limit == charge_limit_target or not my_predbat.allow_this_charge_window(window_n):
                    continue
                my_predbat.charge_limit_best[window_n] = charge_limit_target
                my_predbat.charge_limit_best[window_n_target] = charge_limit
                best_metric = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[0]
                tried += 1
                if best_metric < (selected_metric - min_improvement_swap):
                    selected_metric = best_metric
                    swapped = True
                    swapped_target[window_n_target] = True
                    kept += 1
                    if tried > 1:
                        kept_later_partner += 1
                    break
                my_predbat.charge_limit_best[window_n] = charge_limit
                my_predbat.charge_limit_best[window_n_target] = charge_limit_target
    return kept, kept_later_partner


def test_batched_metric_matches_run_prediction_metric(my_predbat, kernel_modes):
    """A metric queued through launch_run_prediction_metric is the one run_prediction_metric returns, returns True on failure.

    Equal, not close: the swap pass compares metrics against a threshold, so a batched metric that
    differed in the last digit could keep or drop a different swap. Checked on the Python engine and
    the kernel, with and without the PV90 scenario, on plans with a target export so the PV10 run is
    floored from the nominal one on both paths.
    """
    print("**** Running batched prediction metric tests ****")
    failed = False
    for kernel in kernel_modes:
        for pv90_weight in (0.0, 0.2):
            for seed in range(4):
                charge_limit = setup_swap_scenario(my_predbat, seed, kernel, pv90_weight=pv90_weight)
                if kernel and not my_predbat.prediction.kernel_handle:
                    print("ERROR: seed {} expected the prediction kernel but it is not in use".format(seed))
                    failed = True
                args = (charge_limit, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best)
                single = my_predbat.run_prediction_metric(*args, end_record=my_predbat.end_record)

                handles = my_predbat.launch_run_prediction_metric(*args, my_predbat.end_record)
                want_jobs = 3 if pv90_weight > 0 else 2
                if len(my_predbat.prediction.pending_batch) != want_jobs:
                    print("ERROR: kernel {} pv90 weight {} queued {} jobs, expected {} waiting to run".format(kernel, pv90_weight, len(my_predbat.prediction.pending_batch), want_jobs))
                    failed = True
                if (handles[2] is not None) != (pv90_weight > 0):
                    print("ERROR: kernel {} pv90 weight {} PV90 should only be queued when it is weighted".format(kernel, pv90_weight))
                    failed = True
                batched = my_predbat.get_run_prediction_metric(handles)
                if batched != single:
                    print("ERROR: kernel {} pv90 weight {} seed {} batched metric {} differs from run_prediction_metric {}".format(kernel, pv90_weight, seed, batched, single))
                    failed = True
    if not failed:
        print("Batched prediction metric tests passed")
    return failed


def test_swap_charge_matches_one_at_a_time(my_predbat, kernel_modes):
    """The batched pass ends on the plan the one-at-a-time search ends on, returns True on failure.

    Batching queues every partner of a target before it knows which swap will be kept, so the two
    could part company if a result were read out of order, or if a kept swap did not reach the plan
    the later targets are tried against. Both show up as different final limits, so the limits are
    compared over seeded plans, on the Python engine and on the kernel at one and four threads. The
    plans must between them keep some swaps, some of them with a partner other than the first tried,
    or the comparison would pass without having tested the order.
    """
    print("**** Running batched swap charge search tests ****")
    failed = False
    total_kept = 0
    total_later_partner = 0
    for kernel in kernel_modes:
        for pv90_weight in (0.0, 0.2):
            for seed in range(8):
                start = setup_swap_scenario(my_predbat, seed, kernel, pv90_weight=pv90_weight)
                kept, kept_later_partner = swap_charge_one_at_a_time(my_predbat, len(start))
                expected = list(my_predbat.charge_limit_best)
                total_kept += kept
                total_later_partner += kept_later_partner

                for threads in (1, 4) if kernel else (1,):
                    my_predbat.charge_limit_best = list(start)
                    my_predbat.prediction.batch_threads = threads
                    my_predbat.prediction.prediction_cache = {}
                    my_predbat.optimise_swap_charge(len(start))
                    if my_predbat.charge_limit_best != expected:
                        print("ERROR: kernel {} threads {} pv90 weight {} seed {} batched pass ended on {} but one at a time ended on {} (from {})".format(kernel, threads, pv90_weight, seed, my_predbat.charge_limit_best, expected, start))
                        failed = True
                    if my_predbat.prediction.pending_batch:
                        print("ERROR: kernel {} threads {} seed {} the pass left {} jobs queued".format(kernel, threads, seed, len(my_predbat.prediction.pending_batch)))
                        failed = True
    if total_kept == 0 or total_later_partner == 0:
        print("ERROR: the plans kept {} swaps, {} with a later partner - too few to test the order".format(total_kept, total_later_partner))
        failed = True
    if not failed:
        print("Batched swap charge search tests passed - {} swaps kept, {} with a later partner".format(total_kept, total_later_partner))
    return failed


def test_swap_charge_runs_on_kernel_batch(my_predbat):
    """With the kernel on, every swap trial reaches it as a batch with the configured thread count, returns True on failure.

    This is the point of the pass queueing its trials. It fails if the pass prices a trial through
    run_prediction_metric again, if any queued job falls back to the Python engine, if the thread
    count set from the threads option does not reach the kernel call, or if each trial is flushed on
    its own (a metric is two jobs here, so a batch of more than two holds more than one trial).
    """
    print("**** Running swap charge kernel batch tests ****")
    failed = False
    start = setup_swap_scenario(my_predbat, 1, True)
    prediction = my_predbat.prediction
    prediction.batch_threads = 3

    kernel_calls = []
    single_calls = []
    python_jobs = []
    real_batch = prediction_batch.run_prediction_kernel_batch
    real_metric = my_predbat.run_prediction_metric
    real_python_job = prediction.run_batch_job_python

    def spy_batch(pred, jobs, n_threads):
        """Record the size and thread count of each kernel batch"""
        kernel_calls.append((len(jobs), n_threads))
        return real_batch(pred, jobs, n_threads)

    def spy_metric(*args, **kwargs):
        """Record a metric priced outside the batch"""
        single_calls.append(1)
        return real_metric(*args, **kwargs)

    def spy_python_job(job):
        """Record a queued job the kernel did not take"""
        python_jobs.append(1)
        return real_python_job(job)

    prediction_batch.run_prediction_kernel_batch = spy_batch
    my_predbat.run_prediction_metric = spy_metric
    prediction.run_batch_job_python = spy_python_job
    try:
        my_predbat.optimise_swap_charge(len(start))
    finally:
        prediction_batch.run_prediction_kernel_batch = real_batch
        del my_predbat.run_prediction_metric
        del prediction.run_batch_job_python

    if not kernel_calls:
        print("ERROR: the swap pass made no kernel batch call")
        failed = True
    if any(n_threads != 3 for _, n_threads in kernel_calls):
        print("ERROR: kernel batches should all use the 3 configured threads, got {}".format(sorted(set(n_threads for _, n_threads in kernel_calls))))
        failed = True
    if kernel_calls and max(n_jobs for n_jobs, _ in kernel_calls) <= 2:
        print("ERROR: no kernel batch held more than one trial, sizes {}".format([n_jobs for n_jobs, _ in kernel_calls]))
        failed = True
    if single_calls:
        print("ERROR: the swap pass priced {} metrics through run_prediction_metric instead of the batch".format(len(single_calls)))
        failed = True
    if python_jobs:
        print("ERROR: {} queued jobs fell back to the Python engine with the kernel on".format(len(python_jobs)))
        failed = True
    if not failed:
        print("Swap charge kernel batch tests passed - {} batches, largest {} jobs".format(len(kernel_calls), max(n_jobs for n_jobs, _ in kernel_calls)))
    return failed


def run_swap_charge_batch_tests(my_predbat):
    """Run the batch tests on the Python engine and, where it is available, the kernel, returns True on failure."""
    available, required_failure = kernel_available()
    if required_failure:
        return True
    kernel_modes = [False, True] if available else [False]

    saved = (my_predbat.pv_metric90_weight, my_predbat.prediction_kernel_enable, my_predbat.debug_enable)
    failed = False
    try:
        failed |= test_batched_metric_matches_run_prediction_metric(my_predbat, kernel_modes)
        failed |= test_swap_charge_matches_one_at_a_time(my_predbat, kernel_modes)
        if available:
            failed |= test_swap_charge_runs_on_kernel_batch(my_predbat)
        else:
            print("WARNING: Prediction kernel not available - swap charge kernel batch test SKIPPED")
    finally:
        my_predbat.pv_metric90_weight, my_predbat.prediction_kernel_enable, my_predbat.debug_enable = saved
    return failed
