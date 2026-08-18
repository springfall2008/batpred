# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the batched prediction fan-out.

launch_run_prediction_* no longer runs anything: it queues a job on the Prediction and returns a
handle whose first get() flushes the whole batch through one pk_run_batch call. These tests pin what
that deferral relies on: that a trial owns its inputs rather than sharing the caller's, that a queued
job returns exactly what the direct thread_run_prediction_* path returns for every job shape and step,
that nothing runs before the first get() and one get() drains the batch, that cache hits and
intra-batch duplicates never reach the kernel while min/max jobs are never cached at all, that every
job the kernel will not take still runs on the engine the direct path would have used, and that every
handle in a multi-job batch comes back with its own job's result rather than a neighbour's.
"""


import random

import prediction_batch
from const import PREDICT_STEP, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10
from plan import resolve_batch_threads
from prediction import Prediction
from prediction_kernel import create_kernel_context
from tests.test_kernel_parity import apply_random_scenario, kernel_available, make_step_data, make_windows, restore_scenario_state, snapshot_scenario_state


def make_export_windows(minutes_now):
    """Build a small deterministic export window layout for the trial-input tests"""
    return [
        {"start": minutes_now + 60, "end": minutes_now + 120, "average": 15.0},
        {"start": minutes_now + 180, "end": minutes_now + 240, "average": 20.0},
    ]


def test_export_trial_does_not_mutate_caller_window(my_predbat):
    """The export trial must build its own window list, returns True on failure.

    It used to write the trial start straight into the caller's window dict, which was only safe
    because each pool worker mutated its own unpickled copy. A batched fan-out holds one shared list
    across every job in the batch, so an in-place write would corrupt every other trial of the same
    window.
    """
    print("**** Running export trial input tests ****")
    failed = False
    minutes_now = my_predbat.minutes_now
    export_window = make_export_windows(minutes_now)
    export_limits = [100.0, 100.0]
    original = [dict(window) for window in export_window]

    prediction = Prediction(my_predbat, {}, {}, {}, {})
    trial_window, trial_limits = prediction._prepare_export(5.0, minutes_now + 90, 0, export_window, export_limits, None)

    if export_window != original:
        print("ERROR: _prepare_export mutated the caller's export window: {} vs {}".format(export_window, original))
        failed = True
    if export_limits != [100.0, 100.0]:
        print("ERROR: _prepare_export mutated the caller's export limits: {}".format(export_limits))
        failed = True
    if trial_window[0]["start"] != minutes_now + 90:
        print("ERROR: trial window start not applied, got {}".format(trial_window[0]["start"]))
        failed = True
    if trial_window[1] is not export_window[1]:
        print("ERROR: untouched windows should be shared with the caller's list, not copied")
        failed = True
    if trial_limits[0] != 5.0:
        print("ERROR: trial export limit not applied, got {}".format(trial_limits[0]))
        failed = True

    # The trial start is clamped to at least 5 minutes before the window end
    trial_window, _ = prediction._prepare_export(5.0, minutes_now + 200, 0, export_window, export_limits, None)
    if trial_window[0]["start"] != minutes_now + 115:
        print("ERROR: trial window start not clamped to end-5, got {}".format(trial_window[0]["start"]))
        failed = True

    if not failed:
        print("Export trial input tests passed")
    return failed


def test_batch_state_exists_without_a_base(my_predbat):
    """A Prediction built without a base must still be able to queue, returns True on failure.

    pending_batch and batch_threads used to be initialised only inside __init__'s `if base:` branch,
    so a baseless Prediction raised AttributeError on its first enqueue. Nothing constructs one today
    - the wrapped_* trampolines that did went with the process pool - which is exactly why the trap
    is easy to walk back into.
    """
    print("**** Running baseless Prediction tests ****")
    failed = False
    prediction = Prediction()

    if prediction.pending_batch != []:
        print("ERROR: a baseless Prediction has pending_batch {}".format(prediction.pending_batch))
        failed = True
    if prediction.batch_threads != 1:
        print("ERROR: a baseless Prediction has batch_threads {}".format(prediction.batch_threads))
        failed = True

    handle = prediction.enqueue_prediction([], [], [], [], PV_SCENARIO_NOMINAL, 0, PREDICT_STEP, False)
    if len(prediction.pending_batch) != 1:
        print("ERROR: enqueue on a baseless Prediction left {} pending job(s)".format(len(prediction.pending_batch)))
        failed = True
    if handle.job.result is not None:
        print("ERROR: enqueue on a baseless Prediction ran the job")
        failed = True

    if not failed:
        print("Baseless Prediction tests passed")
    return failed


def make_batch_prediction(my_predbat, seed=7):
    """Build a kernel-enabled Prediction plus a charge/export window layout to fan out over"""
    rng = random.Random(seed)
    apply_random_scenario(my_predbat, rng)
    pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, rng)
    my_predbat.prediction_kernel_enable = True
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
    prediction.kernel_handle = create_kernel_context(prediction)
    charge_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, 4)
    export_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, 4)
    charge_limit = [round(my_predbat.soc_max / 2, 2)] * len(charge_window)
    export_limits = [100.0] * len(export_window)
    return prediction, charge_window, export_window, charge_limit, export_limits


def test_queued_matches_direct(my_predbat):
    """Every queued prediction must return exactly what the direct thread path returns, returns True on failure"""
    print("**** Running queued vs direct equivalence tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes
    try_soc = round(my_predbat.soc_max * 0.8, 2)

    cases = [
        (
            "single",
            lambda: prediction.thread_run_prediction_single(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, 5),
            lambda: prediction.queue_run_prediction_single(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, 5),
        ),
        (
            "charge",
            lambda: prediction.thread_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, None, end_record),
            lambda: prediction.queue_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, None, end_record),
        ),
        (
            "charge_min_max",
            lambda: prediction.thread_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record),
            lambda: prediction.queue_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record),
        ),
        (
            "charge_min_max_all_n",
            lambda: prediction.thread_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, [0, 2], end_record),
            lambda: prediction.queue_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, [0, 2], end_record),
        ),
        (
            "export",
            lambda: prediction.thread_run_prediction_export(5.0, export_window[1]["start"] + 15, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record),
            lambda: prediction.queue_run_prediction_export(5.0, export_window[1]["start"] + 15, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record),
        ),
        # The levels optimiser runs single predictions at a coarse "fast mode" step (plan_interval_minutes,
        # 30 by default), which is the only caller that passes a step other than PREDICT_STEP. It reaches
        # both the cache key and the job, so it needs its own case
        (
            "single_coarse_step",
            lambda: prediction.thread_run_prediction_single(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, 30),
            lambda: prediction.queue_run_prediction_single(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, 30),
        ),
    ]

    for name, direct_call, queue_call in cases:
        # The queued run goes first, so the direct run cannot be answered out of the cache the queued
        # one just populated - and vice versa, hence the clear between them
        prediction.prediction_cache = {}
        queued = queue_call().get()
        prediction.prediction_cache = {}
        direct = direct_call()
        if queued != direct:
            print("ERROR: {} queued {} != direct {}".format(name, queued, direct))
            failed = True

    if not failed:
        print("Queued predictions match the direct path for all {} shapes".format(len(cases)))
    return failed


def test_batch_is_lazy(my_predbat):
    """Nothing runs until the first get(), and one get() flushes the whole batch, returns True on failure"""
    print("**** Running batch laziness tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes

    handles = [prediction.queue_run_prediction_charge(round(soc, 2), 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record) for soc in (1.0, 2.0, 3.0)]
    if len(prediction.pending_batch) != 3:
        print("ERROR: expected 3 pending jobs, got {}".format(len(prediction.pending_batch)))
        failed = True
    if any(handle.job.result is not None for handle in handles):
        print("ERROR: a job ran before its handle was read")
        failed = True

    handles[0].get()
    if prediction.pending_batch:
        print("ERROR: pending batch not drained by the first get(), {} left".format(len(prediction.pending_batch)))
        failed = True
    if any(handle.job.result is None for handle in handles):
        print("ERROR: one get() did not flush the whole batch")
        failed = True

    # A second read must not re-run anything
    before = handles[2].job.result
    if handles[2].get() is not before:
        print("ERROR: reading a handle twice recomputed the result")
        failed = True

    if not failed:
        print("Batch laziness tests passed")
    return failed


def test_batch_cache_and_dedup(my_predbat):
    """Cache hits and intra-batch duplicates must never reach the kernel, returns True on failure"""
    print("**** Running batch cache/dedup tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes
    prediction.prediction_cache_enable = True

    calls = []
    real_batch = prediction_batch.run_prediction_kernel_batch

    def spy(pred, jobs, n_threads):
        """Record how many jobs each batch call was handed, then run it for real"""
        calls.append(len(jobs))
        return real_batch(pred, jobs, n_threads)

    prediction_batch.run_prediction_kernel_batch = spy
    try:
        # Three identical jobs in one batch must collapse to one kernel job
        prediction.prediction_cache = {}
        handles = [prediction.queue_run_prediction_charge(4.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record) for _ in range(3)]
        results = [handle.get() for handle in handles]
        if calls != [1]:
            print("ERROR: expected one kernel job for three identical trials, got {}".format(calls))
            failed = True
        if results[0] != results[1] or results[1] != results[2]:
            print("ERROR: deduplicated jobs returned different results")
            failed = True

        # And a repeat in a later batch must come from the cache without a kernel call at all
        del calls[:]
        again = prediction.queue_run_prediction_charge(4.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
        if calls:
            print("ERROR: a cached scenario still reached the kernel: {}".format(calls))
            failed = True
        if again != results[0]:
            print("ERROR: cached result {} differs from the computed one {}".format(again, results[0]))
            failed = True

        # A min/max job must never be cached. The SoC range is not part of a cached result, so a hit
        # would answer (0.0, 0.0), collapse the caller's SoC pruning envelope and silently drop
        # candidate SoCs from the search. Checked twice: through the public entry point, and by
        # asking enqueue_prediction for a cached range job directly - the second is what pins the
        # guard as structural rather than a property of the one call site that passes cache=False.
        prediction.prediction_cache = {}
        for _ in range(2):
            prediction.queue_run_prediction_charge_min_max(4.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
        if prediction.prediction_cache:
            print("ERROR: a queued min/max job populated the prediction cache: {}".format(len(prediction.prediction_cache)))
            failed = True

        range_handle = prediction.enqueue_prediction(list(charge_limit), charge_window, export_window, list(export_limits), PV_SCENARIO_NOMINAL, end_record, PREDICT_STEP, True, want_range=True, range_window=charge_window[1])
        if range_handle.job.sim_hash is not None:
            print("ERROR: a range job was given a cache key despite never being cacheable")
            failed = True
        range_handle.get()
        if prediction.prediction_cache:
            print("ERROR: a range job enqueued with cache=True populated the prediction cache: {}".format(len(prediction.prediction_cache)))
            failed = True
    finally:
        prediction_batch.run_prediction_kernel_batch = real_batch

    if not failed:
        print("Batch cache/dedup tests passed")
    return failed


def test_queued_range_window_in_the_past(my_predbat):
    """A range window that started before minutes_now must give the direct path's answer, returns True on failure.

    make_windows only ever emits windows starting at or after minutes_now, so the max(..., 0) clamp on
    the range start step and the negative end step of a wholly-past window are otherwise never
    executed. Both matter: the kernel derives the range from those two step numbers while a fallback
    derives it from scan_soc_range, and the two have to agree exactly.
    """
    print("**** Running past range window tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes
    minutes_now = my_predbat.minutes_now
    window = charge_window[1]

    cases = [
        # Straddles minutes_now: the start step clamps to 0, the end step is positive
        ("straddling", {"start": minutes_now - 120, "end": window["end"], "average": window["average"]}),
        # Wholly behind minutes_now: both steps are negative, so no step is ever in range
        ("wholly past", {"start": minutes_now - 240, "end": minutes_now - 60, "average": window["average"]}),
        # Ends exactly at minutes_now, the boundary between the two cases above
        ("ends at now", {"start": minutes_now - 180, "end": minutes_now, "average": window["average"]}),
    ]

    for name, past_window in cases:
        trial_window = list(charge_window)
        trial_window[1] = past_window
        prediction.prediction_cache = {}
        queued = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, trial_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
        prediction.prediction_cache = {}
        direct = prediction.thread_run_prediction_charge_min_max(3.0, 1, charge_limit, trial_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
        if queued != direct:
            print("ERROR: {} range window queued {} != direct {}".format(name, queued, direct))
            failed = True

    if not failed:
        print("Past range window tests passed for all three shapes")
    return failed


def test_batch_fallbacks(my_predbat):
    """A job the kernel cannot take still runs, and gives the Python engine's answer, returns True on failure"""
    print("**** Running batch fallback tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes

    # Two references, because the fallbacks land on two different engines. A batch the kernel refused
    # still re-runs through run_prediction, which dispatches to the kernel; a Prediction the kernel
    # will not serve at all re-runs on the Python engine. Those two agree to 1e-6 (kernel_parity), not
    # bit-exactly, so each fallback is compared against the engine it actually used.
    prediction.prediction_cache = {}
    expected = prediction.thread_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    prediction.debug_enable = True
    prediction.prediction_cache = {}
    expected_python = prediction.thread_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    prediction.debug_enable = False

    # 1. Whole batch refused (no kernel, stale binary, or a non-zero return code)
    real_batch = prediction_batch.run_prediction_kernel_batch

    def refuse_all(pred, jobs, n_threads):
        """Stand in for a kernel that cannot run the batch at all"""
        return None

    def refuse_each(pred, jobs, n_threads):
        """Stand in for a kernel that ran the batch but rejected every job"""
        return [(None, 0.0, 0.0)] * len(jobs)

    for name, stub in (("whole batch", refuse_all), ("per job", refuse_each)):
        prediction_batch.run_prediction_kernel_batch = stub
        try:
            prediction.prediction_cache = {}
            got = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
            if got != expected:
                print("ERROR: {} fallback returned {} expected {}".format(name, got, expected))
                failed = True
        finally:
            prediction_batch.run_prediction_kernel_batch = real_batch

    # 2. Debug runs must not reach the kernel from the batch when the direct path would not take it.
    # This has to assert the mechanism, not the value: the two engines agree to ~1e-6 and every field
    # is rounded to 4dp, so the tuples match whether or not the kernel_supported() gate exists. The
    # gate is load-bearing because a debug run needs predict_soc_best and friends populated for the
    # debug HTML, and reset_kernel_run_state clears exactly those.
    calls = []

    def spy_batch(pred, jobs, n_threads):
        """Record that the kernel batch was reached at all, then run it for real"""
        calls.append(len(jobs))
        return real_batch(pred, jobs, n_threads)

    prediction.debug_enable = True
    prediction_batch.run_prediction_kernel_batch = spy_batch
    try:
        prediction.prediction_cache = {}
        got = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
        if calls:
            print("ERROR: a debug run reached the kernel batch: {}".format(calls))
            failed = True
        if got != expected_python:
            print("ERROR: debug_enable fallback returned {} expected {}".format(got, expected_python))
            failed = True
    finally:
        prediction_batch.run_prediction_kernel_batch = real_batch
        prediction.debug_enable = False

    # 3. Kernel genuinely unavailable for this Prediction
    prediction.kernel_handle = 0
    prediction.prediction_cache = {}
    got = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
    if got != expected_python:
        print("ERROR: kernel-less fallback returned {} expected {}".format(got, expected_python))
        failed = True

    if not failed:
        print("Batch fallback tests passed")
    return failed


def test_save_run_drains_pending_batch(my_predbat):
    """A saving run must flush any pending batch before it runs, returns True on failure.

    A save run is the only one that populates predict_soc_best and friends, and it goes down the
    Python engine because kernel_supported() refuses save. If a batch were left pending across it,
    the next handle read would flush it and reset_kernel_run_state would blank exactly those
    attributes - the published plan and the debug HTML would come back empty. All four fan-outs drain
    their handles before any save run today, so this pins the guard, not a live bug: it is what stops
    a fifth fan-out from introducing one.
    """
    print("**** Running save-run batch drain tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes

    handle = prediction.queue_run_prediction_charge(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    if handle.job.result is not None:
        print("ERROR: the queued job ran before the save run")
        failed = True

    prediction.run_prediction(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record=end_record, save="best")

    if prediction.pending_batch:
        print("ERROR: the save run left {} job(s) pending".format(len(prediction.pending_batch)))
        failed = True
    if handle.job.result is None:
        print("ERROR: the save run did not flush the pending job")
        failed = True
    if not prediction.predict_soc_best:
        print("ERROR: the save run published no predict_soc_best, so this test cannot see it being blanked")
        failed = True

    # The point of the guard: reading the handle afterwards must not wipe what the save run published
    published = dict(prediction.predict_soc_best)
    handle.get()
    if prediction.predict_soc_best != published:
        print("ERROR: reading a handle after a save run changed predict_soc_best ({} entries, was {})".format(len(prediction.predict_soc_best), len(published)))
        failed = True

    if not failed:
        print("Save-run batch drain tests passed")
    return failed


def test_batch_results_match_their_own_job(my_predbat):
    """Every handle in a multi-job batch must get its own job's result, at any thread count, returns True on failure.

    The oracle is external, and it has to be: flush_batch resolves cache hits and intra-batch
    duplicates before the kernel call and then walks the surviving jobs alongside the kernel's
    results, so an off-by-one or reversed job->result mapping is easy to introduce and returns a
    perfectly plausible cost for every trial. Comparing a batch against another batch cannot see
    that - both would be wrong the same way - and neither can sweeping n_threads, which is forwarded
    verbatim to C with nothing on the Python side varying with it.

    So each handle is compared against thread_run_prediction_charge for the same try_soc, with the
    prediction cache cleared between the two runs so neither can be answered out of what the other
    stored. The SoCs are checked to give genuinely distinct costs first, otherwise a misrouted result
    could match by coincidence.

    The thread sweep rides along: pk_run_batch is pinned bit-identical across thread counts at the C
    level (kernel_parity), so this pins that the Python side routes results the same way however the
    kernel split the work.
    """
    print("**** Running batch result routing tests ****")
    failed = False
    # Seed 5 rather than the shared default: this test needs ten trials with ten different costs, and
    # the default scenario's battery saturates so that nine of the ten come out identical
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat, seed=5)
    end_record = my_predbat.forecast_minutes
    socs = [round(my_predbat.soc_max * fraction, 2) for fraction in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)]
    # Every charge window takes the trial SoC, not just one: moving a single window's limit saturates
    # once that window can no longer absorb more, which collapses the costs back together
    all_n = list(range(len(charge_window)))

    # The oracle: one direct run per SoC, each from a cold cache so it is genuinely simulated
    expected = []
    for soc in socs:
        prediction.prediction_cache = {}
        expected.append(prediction.thread_run_prediction_charge(soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record))

    costs = [result[0] for result in expected]
    if len(set(costs)) != len(costs):
        print("ERROR: the trial SoCs do not all give distinct costs ({}) - a misrouted result could match by luck".format(costs))
        failed = True

    for n_threads in (1, 2, 8):
        prediction.batch_threads = n_threads
        prediction.prediction_cache = {}
        handles = [prediction.queue_run_prediction_charge(soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record) for soc in socs]
        results = [handle.get() for handle in handles]
        for index, (got, want) in enumerate(zip(results, expected)):
            if got != want:
                print("ERROR: job {} (try_soc {}) at {} threads returned {} expected {}".format(index, socs[index], n_threads, got, want))
                failed = True
    prediction.batch_threads = 1

    if not failed:
        print("Batch result routing tests passed - {} jobs matched their own direct run at 1, 2 and 8 threads".format(len(socs)))
    return failed


def test_batch_thread_count_resolution():
    """Check how the threads setting maps onto kernel lanes, returns True on failure.

    'auto' takes the core count uncapped. A cap was measured and rejected: on a fast machine the
    curve is flat and peaks slightly below the core count, but with each job made eight times dearer
    - how a machine where the kernel dominates behaves - it stops turning over, and a cap of 4 then
    costs 10.7% against 1.3% for no cap at all. The zero and one cases both mean serial, which is what
    annual.py and the benchmark harness rely on.
    """
    print("**** Running batch thread count resolution tests ****")
    failed = False
    cases = [
        ("auto", 2, 2, "auto follows the core count"),
        ("auto", 16, 16, "auto is not capped"),
        ("auto", 64, 64, "auto is not capped on a large machine either"),
        ("auto", 0, 1, "a nonsense core count still leaves one usable lane"),
        (0, 32, 1, "0 means serial"),
        (1, 32, 1, "1 means serial"),
        (2, 32, 2, "an explicit value is honoured"),
        (12, 32, 12, "an explicit value above the core count is honoured"),
    ]
    for threads, cores, expected, why in cases:
        got = resolve_batch_threads(threads, cores)
        if got != expected:
            print("ERROR: threads={} cores={} gave {} lanes, expected {} ({})".format(threads, cores, got, expected, why))
            failed = True

    if not failed:
        print("Batch thread count resolution tests passed")
    return failed


def run_prediction_batch_tests(my_predbat):
    """Run every batched prediction test, returns True on failure"""
    failed = test_export_trial_does_not_mutate_caller_window(my_predbat)
    failed |= test_batch_thread_count_resolution()
    failed |= test_batch_state_exists_without_a_base(my_predbat)

    available, required_failure = kernel_available()
    if not available:
        print("WARNING: kernel not available - batch tests that need it are SKIPPED")
        return failed or required_failure

    state = snapshot_scenario_state(my_predbat)
    try:
        failed |= test_queued_matches_direct(my_predbat)
        failed |= test_queued_range_window_in_the_past(my_predbat)
        failed |= test_batch_is_lazy(my_predbat)
        failed |= test_batch_cache_and_dedup(my_predbat)
        failed |= test_batch_fallbacks(my_predbat)
        failed |= test_save_run_drains_pending_batch(my_predbat)
        failed |= test_batch_results_match_their_own_job(my_predbat)
    finally:
        restore_scenario_state(my_predbat, state)
        my_predbat.prediction_kernel_enable = False

    if failed:
        print("**** Prediction batch tests FAILED ****")
    else:
        print("**** Prediction batch tests passed ****")
    return failed
