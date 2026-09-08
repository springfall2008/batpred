# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Lazy batching of the prediction fan-out.

Every optimiser fan-out in plan.py is launch-all-then-collect-all, so the launches can queue instead
of running: each one prepares its trial inputs, appends a BatchJob and returns a BatchHandle, and the
first get() runs the whole batch through a single pk_run_batch call. That pays the Python/C boundary
and the window marshalling once per fan-out instead of once per scenario, and it is the only shape
that lets the kernel spread the work across cores - a Python thread pool cannot (the GIL serialises
it) and a process pool cannot (the parent's pickling is a serial bottleneck); both were measured and
rejected.

INVARIANT: a caller must not mutate any list or window dict it has handed to a queue_* entry point
until it has read the matching handle. The four fan-outs in plan.py satisfy this; the 20-scenario
byte-identical plan comparison is what keeps it that way.
"""

from const import PREDICT_STEP
from prediction_kernel import BatchJob, kernel_supported, run_prediction_kernel_batch, window_bound_tuple


def prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step):
    """Hash a simulation's inputs into its prediction cache key.

    Shared by Prediction.run_prediction and the batch runner so a scenario cannot land under two
    different keys depending on which path reached it. Built as one tuple hash to keep the per-window
    hashing in C rather than looping in Python, which matters because it runs on every simulation
    with a few hundred windows.

    The window bound tuples come from the identity-keyed cache in prediction_kernel, so a fan-out
    that varies only the limits derives them once rather than once per simulation. That cache is kept
    honest by set_window_start/set_window_end invalidating it; run_window_cache_tests replays a full
    plan with VALIDATE_WINDOW_CACHE on to prove no mutation escapes them.
    """
    return hash(
        (
            tuple(charge_limit),
            window_bound_tuple(charge_window),
            tuple(export_limits),
            window_bound_tuple(export_window),
            pv_scenario,
            end_record,
            step,
        )
    )


def shape_batch_result(job, result, soc_range_min, soc_range_max):
    """Shape a full run_prediction result into what this job's thread_run_prediction_* returned"""
    shaped = tuple(result[:11])
    if job.want_range:
        shaped = shaped + (soc_range_min, soc_range_max)
    return shaped


class BatchHandle:
    """Result handle for a queued prediction: get() runs the batch if it has not run yet.

    Deliberately shaped like a future so a fan-out reads as launch-all-then-get-all, but nothing is
    running in the background - the work happens inside the first get().
    """

    __slots__ = ("pred", "job")

    def __init__(self, pred, job):
        """Bind the handle to the Prediction that owns the batch and to its own job"""
        self.pred = pred
        self.job = job

    def get(self):
        """Return this job's result, flushing the whole pending batch on the first call"""
        if self.job.result is None:
            self.pred.flush_batch()
        return self.job.result


class PredictionBatch:
    """Pending-batch state and flushing for Prediction.

    A mixin rather than a separate object so the pending list lives directly on the Prediction that
    owns the kernel context, with no reference cycle between the two.
    """

    def enqueue_prediction(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache, want_range=False, range_window=None):
        """Queue one prediction and return a handle to its eventual result"""
        # A range job is never cached, and that is structural rather than the caller's choice: the
        # SoC range is not part of a cached result, so a hit would answer with (0.0, 0.0) and silently
        # collapse the caller's SoC pruning envelope. Callers pass cache=False today; ignoring cache
        # outright for a range job means a future caller cannot reintroduce that bug.
        sim_hash = prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step) if cache and not want_range else None

        # The kernel tracks the SoC range itself over a step range, so a min/max job does not have to
        # ship an 8928-entry SoC buffer. A negative start step means "no range asked for", which the
        # kernel answers with (soc_max, 0) - exactly what the Python all_n path returns unclamped.
        soc_range_start_step = -1
        soc_range_end_step = -1
        if range_window is not None:
            predict_minute_start = max(int((range_window["start"] - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((range_window["end"] - self.minutes_now) / 5) * 5
            soc_range_start_step = predict_minute_start // PREDICT_STEP
            soc_range_end_step = predict_minute_end // PREDICT_STEP

        job = BatchJob(
            charge_limit,
            charge_window,
            export_window,
            export_limits,
            pv_scenario,
            end_record,
            step,
            soc_range_start_step=soc_range_start_step,
            soc_range_end_step=soc_range_end_step,
            sim_hash=sim_hash,
            want_range=want_range,
            range_window=range_window,
        )
        self.pending_batch.append(job)
        return BatchHandle(self, job)

    def run_batch_job_python(self, job):
        """Run one job through the normal prediction path, for when the kernel cannot take it"""
        result = self.run_prediction(job.charge_limit, job.charge_window, job.export_window, job.export_limits, job.pv_scenario, end_record=job.end_record, step=job.step, cache=job.sim_hash is not None)
        if not job.want_range:
            return tuple(result[:11])
        if job.range_window is None:
            return tuple(result[:11]) + (self.soc_max, 0)
        return tuple(result[:11]) + self.scan_soc_range(result[11], job.range_window)

    def flush_batch(self):
        """Run every pending job: cache hits first, then one kernel call, then any fallbacks"""
        pending = self.pending_batch
        if not pending:
            return
        # Cleared before anything runs, so a fallback that re-enters run_prediction cannot see a
        # half-flushed batch or flush it a second time
        self.pending_batch = []

        cache = self.prediction_cache
        kernel_jobs = []
        duplicates = []
        first_by_hash = {}
        for job in pending:
            if job.sim_hash is not None:
                cached = cache.get(job.sim_hash)
                if cached is not None:
                    job.result = shape_batch_result(job, cached, 0.0, 0.0)
                    continue
                # Two identical trials in one fan-out used to be a cache hit for the second one; the
                # batch has to collapse them itself or that hit is lost (~44% of calls are hits)
                first = first_by_hash.get(job.sim_hash)
                if first is not None:
                    duplicates.append((job, first))
                    continue
                first_by_hash[job.sim_hash] = job
            kernel_jobs.append(job)

        if kernel_jobs:
            # kernel_supported() is a property of the Prediction, not of the individual job - it also
            # refuses debug runs, which would otherwise reach the kernel here when the direct path
            # would have used the Python engine. save is always None for a queued job: a saving run
            # calls run_prediction directly and never fans out.
            batched = run_prediction_kernel_batch(self, kernel_jobs, self.batch_threads) if kernel_supported(self, None, PREDICT_STEP) else None
            for index, job in enumerate(kernel_jobs):
                entry = batched[index] if batched is not None else None
                if entry is None or entry[0] is None:
                    job.result = self.run_batch_job_python(job)
                    continue
                result, soc_range_min, soc_range_max = entry
                if job.sim_hash is not None:
                    # Stored without the SoC/car data to save memory, mirroring run_prediction
                    cache[job.sim_hash] = result[:11] + ([], []) + result[13:]
                job.result = shape_batch_result(job, result, soc_range_min, soc_range_max)

        for job, first in duplicates:
            job.result = first.result
