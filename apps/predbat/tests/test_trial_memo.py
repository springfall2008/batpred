# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the trial memo in the batched prediction fan-out (PredictionBatch.shared_trial).

A fan-out queues each trial once per PV scenario. The scenarios of one trial now share the trial's
prepared lists, its cache hash and its marshalled charge limit buffer instead of copying, hashing and
marshalling them once per scenario. These tests pin that:

- the scenarios of one trial share their lists, and still return exactly what the direct path returns
  for each scenario, on the kernel and on the Python engine
- the plan is hashed once per trial, not once per scenario
- different trials never share, so a memo key covers everything the preparation reads
- the memo is emptied by every flush, so a caller that changes its list after reading a handle (which
  the batch allows) gets a trial built from the changed list
- the kernel batch marshals each charge limit list once
- the validator catches an input changed between two scenario launches of one trial, and a full plan
  replay with the validator on finds no caller doing that
"""

import prediction_batch
import prediction_kernel
from const import EXPORT_MODE_TARGET, PREDBAT_MAX_CARS, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90
from tests.test_infra import FIXTURE_MINUTES_NOW, reset_inverter
from tests.test_kernel_parity import kernel_available, restore_scenario_state, snapshot_scenario_state
from tests.test_prediction_batch import make_batch_prediction
from utils import pack_export_limit

SCENARIOS = (PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90)


def engine_prediction(my_predbat, kernel, seed=11):
    """make_batch_prediction, switched to the Python engine when kernel is False"""
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat, seed=seed)
    prediction.prediction_cache_enable = True
    if not kernel:
        # kernel_supported() refuses a Prediction without a kernel context, so every job falls back
        prediction.kernel_handle = 0
    return prediction, charge_window, export_window, charge_limit, export_limits


def export_trial(export_window):
    """The export trial these tests queue: window 1 as a target export starting 15 minutes late"""
    return pack_export_limit(EXPORT_MODE_TARGET, 5), export_window[1]["start"] + 15, 1


def test_scenarios_share_one_trial(my_predbat, kernel_modes):
    """The scenarios of one trial share its lists and match the direct path, returns True on failure"""
    print("**** test_scenarios_share_one_trial ****")
    failed = False
    for kernel in kernel_modes:
        prediction, charge_window, export_window, charge_limit, export_limits = engine_prediction(my_predbat, kernel)
        end_record = my_predbat.forecast_minutes
        limit, start, window_n = export_trial(export_window)
        try_soc = round(my_predbat.soc_max * 0.8, 2)
        shapes = {
            "export": (
                lambda pv: prediction.queue_run_prediction_export(limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv, None, end_record),
                lambda pv: prediction.thread_run_prediction_export(limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv, None, end_record),
                ("export_window", "export_limits"),
            ),
            "charge": (
                lambda pv: prediction.queue_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, None, end_record),
                lambda pv: prediction.thread_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, None, end_record),
                ("charge_limit",),
            ),
            "charge_min_max": (
                lambda pv: prediction.queue_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, None, end_record),
                lambda pv: prediction.thread_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, None, end_record),
                ("charge_limit",),
            ),
            "charge_all_n": (
                lambda pv: prediction.queue_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, [0, 2], end_record),
                lambda pv: prediction.thread_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, [0, 2], end_record),
                ("charge_limit",),
            ),
        }
        for name, (queue, direct, shared_fields) in shapes.items():
            label = "kernel={} {}".format(kernel, name)
            prediction.prediction_cache = {}
            prediction.prediction_cache_export_end = {}
            handles = [queue(pv) for pv in SCENARIOS]
            jobs = [handle.job for handle in handles]
            for field in shared_fields:
                values = [getattr(job, field) for job in jobs]
                if not all(value is values[0] for value in values):
                    print("ERROR: {}: the scenarios of one trial should share {}, got {} distinct lists".format(label, field, len({id(value) for value in values})))
                    failed = True
                if values[0] is {"export_window": export_window, "export_limits": export_limits, "charge_limit": charge_limit}[field]:
                    print("ERROR: {}: the trial should own a copy of {}, not the caller's list".format(label, field))
                    failed = True
            queued = [handle.get() for handle in handles]
            if prediction.trial_memo:
                print("ERROR: {}: the flush should empty the trial memo, {} entries left".format(label, len(prediction.trial_memo)))
                failed = True
            for pv, result in zip(SCENARIOS, queued):
                prediction.prediction_cache = {}
                prediction.prediction_cache_export_end = {}
                expect = direct(pv)
                if result != expect:
                    print("ERROR: {} scenario {}: queued {} != direct {}".format(label, pv, result, expect))
                    failed = True
    if not failed:
        print("PASS")
    return failed


def test_plan_hashed_once_per_trial(my_predbat):
    """A cached trial's plan is hashed once for all its scenarios, returns True on failure"""
    print("**** test_plan_hashed_once_per_trial ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = engine_prediction(my_predbat, True)
    end_record = my_predbat.forecast_minutes
    limit, start, window_n = export_trial(export_window)
    calls = []
    real_base = prediction_batch.prediction_cache_base

    def spy(*args):
        """Count the plan hashes made while queueing"""
        calls.append(args)
        return real_base(*args)

    prediction_batch.prediction_cache_base = spy
    try:
        prediction.prediction_cache = {}
        handles = [prediction.queue_run_prediction_export(limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv, None, end_record) for pv in SCENARIOS]
        handles += [prediction.queue_run_prediction_export(limit, start + 5, window_n, charge_limit, charge_window, export_window, export_limits, pv, None, end_record) for pv in SCENARIOS]
        queued_calls = len(calls)
        keys = {handle.job.sim_hash for handle in handles}
        for handle in handles:
            handle.get()
    finally:
        prediction_batch.prediction_cache_base = real_base

    if queued_calls != 2:
        print("ERROR: expected one plan hash per trial (2) while queueing 6 jobs, got {}".format(queued_calls))
        failed = True
    if len(keys) != 6 or None in keys:
        print("ERROR: expected 6 distinct cache keys (2 trials x 3 scenarios), got {}".format(len(keys)))
        failed = True
    if not failed:
        print("PASS")
    return failed


def test_different_trials_do_not_share(my_predbat):
    """Trials that differ in anything the preparation reads get their own lists, returns True on failure"""
    print("**** test_different_trials_do_not_share ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = engine_prediction(my_predbat, True)
    end_record = my_predbat.forecast_minutes
    limit, start, window_n = export_trial(export_window)
    other_limit = pack_export_limit(EXPORT_MODE_TARGET, 40)
    other_limits = list(export_limits)
    other_window = list(export_window)

    exports = {
        "base": (limit, start, window_n, export_window, export_limits, None),
        "start": (limit, start + 5, window_n, export_window, export_limits, None),
        "limit": (other_limit, start, window_n, export_window, export_limits, None),
        "window_n": (limit, export_window[0]["start"], 0, export_window, export_limits, None),
        "all_n": (limit, start, window_n, export_window, export_limits, [0, 1]),
        "limits list": (limit, start, window_n, export_window, other_limits, None),
        "window list": (limit, start, window_n, other_window, export_limits, None),
    }
    jobs = {}
    for name, (this_limit, this_start, this_n, this_window, these_limits, all_n) in exports.items():
        jobs[name] = prediction.queue_run_prediction_export(this_limit, this_start, this_n, charge_limit, charge_window, this_window, these_limits, PV_SCENARIO_NOMINAL, all_n, end_record).job
        expect_window, expect_limits = prediction._prepare_export(this_limit, this_start, this_n, this_window, these_limits, all_n)
        if jobs[name].export_window != expect_window or jobs[name].export_limits != expect_limits:
            print("ERROR: export trial '{}' was given another trial's inputs".format(name))
            failed = True
    for name, job in jobs.items():
        if name != "base" and job.export_limits is jobs["base"].export_limits:
            print("ERROR: export trial '{}' shares its limits with the base trial".format(name))
            failed = True

    charges = {"base": (4.0, 1, charge_limit, None), "soc": (5.0, 1, charge_limit, None), "window_n": (4.0, 2, charge_limit, None), "all_n": (4.0, 1, charge_limit, [1, 2]), "list": (4.0, 1, list(charge_limit), None)}
    charge_jobs = {}
    for name, (try_soc, this_n, this_limit, all_n) in charges.items():
        charge_jobs[name] = prediction.queue_run_prediction_charge(try_soc, this_n, this_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record).job
        if charge_jobs[name].charge_limit != prediction._prepare_charge(try_soc, this_n, this_limit, all_n):
            print("ERROR: charge trial '{}' was given another trial's limits".format(name))
            failed = True
    for name, job in charge_jobs.items():
        if name != "base" and job.charge_limit is charge_jobs["base"].charge_limit:
            print("ERROR: charge trial '{}' shares its limits with the base trial".format(name))
            failed = True
    prediction.flush_batch()
    if not failed:
        print("PASS")
    return failed


def test_memo_emptied_by_flush(my_predbat):
    """A list changed after its handle was read gives a trial built from the change, returns True on failure"""
    print("**** test_memo_emptied_by_flush ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = engine_prediction(my_predbat, True)
    end_record = my_predbat.forecast_minutes
    caller_limit = list(charge_limit)

    first = prediction.queue_run_prediction_charge(4.0, 1, caller_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    first.get()
    # Allowed: the handle has been read
    caller_limit[2] = 0.0
    second = prediction.queue_run_prediction_charge(4.0, 1, caller_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    if second.job.charge_limit is first.job.charge_limit:
        print("ERROR: a trial queued after a flush was handed the trial prepared before it")
        failed = True
    if second.job.charge_limit[2] != 0.0:
        print("ERROR: a trial queued after a flush did not see the caller's change: {}".format(second.job.charge_limit))
        failed = True
    result = second.get()
    prediction.prediction_cache = {}
    expect = prediction.thread_run_prediction_charge(4.0, 1, caller_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    if result != expect:
        print("ERROR: the trial after the change returned {} but the direct path {}".format(result, expect))
        failed = True

    # A flush with nothing pending still empties it
    prediction.trial_memo[("stale",)] = ((), None)
    prediction.flush_batch()
    if prediction.trial_memo:
        print("ERROR: a flush with no pending jobs left the trial memo in place")
        failed = True
    if not failed:
        print("PASS")
    return failed


def test_charge_limit_marshalled_once(my_predbat):
    """The kernel batch marshals each charge limit list once, returns True on failure"""
    print("**** test_charge_limit_marshalled_once ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = engine_prediction(my_predbat, True)
    end_record = my_predbat.forecast_minutes
    marshalled = []
    real_double = prediction_kernel.double_array

    def spy(values):
        """Record each list marshalled into a double buffer"""
        marshalled.append(values)
        return real_double(values)

    prediction_kernel.double_array = spy
    try:
        prediction.prediction_cache = {}
        handles = []
        for try_soc in (2.0, 3.0):
            handles += [prediction.queue_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, pv, None, end_record) for pv in SCENARIOS]
        trial_limits = [handle.job.charge_limit for handle in handles]
        for handle in handles:
            handle.get()
    finally:
        prediction_kernel.double_array = real_double

    per_trial = [sum(1 for values in marshalled if values is limits) for limits in (trial_limits[0], trial_limits[3])]
    if per_trial != [1, 1]:
        print("ERROR: expected each trial's charge limits marshalled once, got {}".format(per_trial))
        failed = True
    if not failed:
        print("PASS")
    return failed


def test_validator_catches_a_changed_input(my_predbat):
    """VALIDATE_TRIAL_MEMO raises when an input changes between two scenarios of one trial, returns True on failure"""
    print("**** test_validator_catches_a_changed_input ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = engine_prediction(my_predbat, True)
    end_record = my_predbat.forecast_minutes
    caller_limit = list(charge_limit)
    prediction_batch.VALIDATE_TRIAL_MEMO = True
    try:
        prediction.queue_run_prediction_charge(4.0, 1, caller_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
        # Not allowed: the handle has not been read
        caller_limit[2] = 0.0
        prediction.queue_run_prediction_charge(4.0, 1, caller_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, None, end_record)
        print("ERROR: VALIDATE_TRIAL_MEMO did not catch an input changed before its handle was read")
        failed = True
    except AssertionError:
        pass
    finally:
        prediction_batch.VALIDATE_TRIAL_MEMO = False
        prediction.pending_batch = []
        prediction.trial_memo = {}
    if not failed:
        print("PASS")
    return failed


def test_plan_replay_with_validator(my_predbat, dump, kernel):
    """Replay a full plan with VALIDATE_TRIAL_MEMO on, so any caller changing an input between scenario launches raises.

    Run on the kernel where it is available: the memo sits in front of either engine, so the engine
    does not change what is validated, and the Python engine makes the replay several times slower.
    """
    print("**** test_plan_replay_with_validator {} kernel={} ****".format(dump, kernel))
    failed = False
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(dump)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()
    my_predbat.args["threads"] = 0
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = kernel
    my_predbat.plan_valid = False

    hits = [0]
    real_shared = prediction_batch.PredictionBatch.shared_trial

    def counting(self, key, inputs, prepare):
        """Count memo hits so the replay is known to have exercised the memo"""
        if key in self.trial_memo:
            hits[0] += 1
        return real_shared(self, key, inputs, prepare)

    prediction_batch.VALIDATE_TRIAL_MEMO = True
    prediction_batch.PredictionBatch.shared_trial = counting
    try:
        my_predbat.calculate_plan(recompute=True)
    except AssertionError as error:
        print("ERROR: stale trial memo during a real plan: {}".format(error))
        failed = True
    finally:
        prediction_batch.VALIDATE_TRIAL_MEMO = False
        prediction_batch.PredictionBatch.shared_trial = real_shared
        my_predbat.prediction_kernel_enable = False

    if not failed and hits[0] < 100:
        print("ERROR: the replay only hit the trial memo {} times, so it proves little".format(hits[0]))
        failed = True
    if not failed:
        print("PASS ({} memo hits validated)".format(hits[0]))
    return failed


def run_trial_memo_tests(my_predbat):
    """Run the trial memo tests, returns True on failure.

    my_predbat should be a fresh instance: the replays at the end load a debug dump over it.
    """
    available, required_failure = kernel_available()
    if required_failure:
        return True
    kernel_modes = [True, False] if available else [False]
    failed = False
    # A fresh instance has no per-car lists until they are sized here, and the random scenario writes to them
    reset_inverter(my_predbat)
    my_predbat.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    state = snapshot_scenario_state(my_predbat)
    try:
        my_predbat.minutes_now = FIXTURE_MINUTES_NOW
        if available:
            failed |= test_scenarios_share_one_trial(my_predbat, kernel_modes)
            failed |= test_plan_hashed_once_per_trial(my_predbat)
            failed |= test_different_trials_do_not_share(my_predbat)
            failed |= test_memo_emptied_by_flush(my_predbat)
            failed |= test_charge_limit_marshalled_once(my_predbat)
            failed |= test_validator_catches_a_changed_input(my_predbat)
        else:
            print("WARNING: Prediction kernel not available - trial memo unit tests SKIPPED")
    finally:
        my_predbat.prediction_kernel_enable = False
        restore_scenario_state(my_predbat, state)

    failed |= test_plan_replay_with_validator(my_predbat, "cases/predbat_debug_agile1.yaml", available)
    failed |= test_plan_replay_with_validator(my_predbat, "cases/predbat_debug_pre_saving1.yaml", available)
    return failed
