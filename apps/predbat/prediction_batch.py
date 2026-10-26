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

import math

from const import EXPORT_MODE_TARGET, PREDICT_STEP, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10
from prediction_kernel import BatchJob, kernel_supported, run_prediction_kernel_batch, window_bound_tuple
from utils import export_mode_of, export_power_of, export_target_of, pack_export_limit


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
    return prediction_cache_combine(prediction_cache_base(charge_limit, charge_window, export_limits, export_window), pv_scenario, end_record, step)


def prediction_cache_base(charge_limit, charge_window, export_limits, export_window):
    """Hash the part of a cache key that does not depend on the scenario - the windows and limits.

    Split out so a pv10 job can find its nominal twin's key by combining its own base with the nominal
    scenario, rather than hashing the whole trial a second time.
    """
    return hash((tuple(charge_limit), window_bound_tuple(charge_window), tuple(export_limits), window_bound_tuple(export_window)))


def prediction_cache_combine(base, pv_scenario, end_record, step):
    """Combine a prediction_cache_base hash with the scenario, record end and step into a cache key"""
    return hash((base, pv_scenario, end_record, step))


def shape_batch_result(job, result, soc_range_min, soc_range_max, export_end_soc=None):
    """Shape a full run_prediction result into what this job's thread_run_prediction_* returned"""
    shaped = tuple(result[:11])
    if job.want_range:
        shaped = shaped + (soc_range_min, soc_range_max)
    if job.want_export_end_soc:
        shaped = shaped + (export_end_soc,)
    return shaped


# The scenarios simulated with each target export floored at where the plan's nominal run ends. pv90 is
# left out: its battery is normally above nominal at the export, where a floor would not bind.
EXPORT_FLOOR_SCENARIOS = (PV_SCENARIO_PV10,)


def floor_export_limits(export_limits, end_socs, soc_max):
    """Raise each target export's target to the SoC the nominal plan reaches at that window's end.

    This is the target clipping sends the inverter, so in a worse scenario the export stops there - and
    a replan shortens it - rather than draining to the target the optimiser trialled. end_socs holds one
    SoC per export window, -1 or None where unknown. The floor is rounded down to a whole percent so it
    never binds in the nominal run it came from, and only ever raises a target. Freeze and idle windows
    carry no target and are left alone.
    """
    floored = list(export_limits)
    if soc_max <= 0:
        return floored
    for window_n, limit in enumerate(export_limits):
        if window_n >= len(end_socs) or export_mode_of(limit) != EXPORT_MODE_TARGET:
            continue
        end_soc = end_socs[window_n]
        if end_soc is None or end_soc < 0:
            continue
        floor = min(int(math.floor(end_soc / soc_max * 100.0)), 100)
        if floor > (export_target_of(limit) or 0):
            floored[window_n] = pack_export_limit(EXPORT_MODE_TARGET, floor, export_power_of(limit))
    return floored


def export_end_soc_from_series(predict_soc, export_window, minutes_now, export_limits):
    """Return the SoC at the end of each target export window, read from a per-minute SoC series.

    Uses the same int((end - minutes_now) / 5) * 5 key the kernel's export_end_soc_out does, so the
    two agree. Only a target export can be floored, so any other window reads -1, as does one ending
    before now or at a step the series does not hold.
    """
    out = []
    for window, limit in zip(export_window, export_limits):
        rel = window["end"] - minutes_now
        if rel < 0 or export_mode_of(limit) != EXPORT_MODE_TARGET:
            out.append(-1.0)
        else:
            out.append(predict_soc.get(int(rel / 5) * 5, -1.0))
    return out


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

    def enqueue_prediction(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache, want_range=False, range_window=None, want_export_end_soc=False):
        """Queue one prediction and return a handle to its eventual result"""
        # A range job is never cached, and that is structural rather than the caller's choice: the
        # SoC range is not part of a cached result, so a hit would answer with (0.0, 0.0) and silently
        # collapse the caller's SoC pruning envelope. Callers pass cache=False today; ignoring cache
        # outright for a range job means a future caller cannot reintroduce that bug.
        base_hash = None
        sim_hash = None
        if cache and not want_range:
            base_hash = prediction_cache_base(charge_limit, charge_window, export_limits, export_window)
            sim_hash = prediction_cache_combine(base_hash, pv_scenario, end_record, step)

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
            want_export_end_soc=want_export_end_soc,
        )
        job.base_hash = base_hash
        self.pending_batch.append(job)
        return BatchHandle(self, job)

    def run_batch_job_python(self, job):
        """Run one job through the normal prediction path, for when the kernel cannot take it"""
        # Window-end SoCs are read from the series, which a cached run does not keep
        wants_end = job.want_export_end_soc or job.need_export_end_soc
        result = self.run_prediction(job.charge_limit, job.charge_window, job.export_window, job.export_limits, job.pv_scenario, end_record=job.end_record, step=job.step, cache=job.sim_hash is not None and not wants_end)
        if not job.want_range:
            shaped = tuple(result[:11])
        elif job.range_window is None:
            shaped = tuple(result[:11]) + (self.soc_max, 0)
        else:
            shaped = tuple(result[:11]) + self.scan_soc_range(result[11], job.range_window)
        if wants_end:
            export_end_soc = export_end_soc_from_series(result[11], job.export_window, self.minutes_now, job.export_limits)
            job.export_end_soc = export_end_soc
            if job.sim_hash is not None:
                self.prediction_cache_export_end[job.sim_hash] = export_end_soc
            if job.export_end_key is not None:
                self.prediction_cache_export_end[job.export_end_key] = export_end_soc
            if job.want_export_end_soc:
                shaped = shaped + (export_end_soc,)
        return shaped

    def run_kernel_jobs(self, jobs, cache):
        """Run jobs through one kernel batch call, falling back to the Python engine per job"""
        if not jobs:
            return
        # kernel_supported() is a property of the Prediction, not of the individual job - it also
        # refuses debug runs, which would otherwise reach the kernel here when the direct path
        # would have used the Python engine. save is always None for a queued job: a saving run
        # calls run_prediction directly and never fans out.
        batched = run_prediction_kernel_batch(self, jobs, self.batch_threads) if kernel_supported(self, None, PREDICT_STEP) else None
        fallbacks = []
        for index, job in enumerate(jobs):
            entry = batched[index] if batched is not None else None
            if entry is None or entry[0] is None:
                fallbacks.append(job)
                continue
            result, soc_range_min, soc_range_max, export_end_soc = entry
            job.export_end_soc = export_end_soc
            if job.export_end_key is not None and export_end_soc is not None:
                self.prediction_cache_export_end[job.export_end_key] = export_end_soc
            if job.sim_hash is not None:
                # Stored without the SoC/car data to save memory, mirroring run_prediction
                cache[job.sim_hash] = result[:11] + ([], []) + result[13:]
                if export_end_soc is not None:
                    self.prediction_cache_export_end[job.sim_hash] = export_end_soc
            job.result = shape_batch_result(job, result, soc_range_min, soc_range_max, export_end_soc)
        # Twins first, so a floored job falling back finds its floor in the memo rather than simulating
        # nominal again; the direct path applies the floor itself
        fallbacks.sort(key=lambda job: job.floor_twin is not None)
        for job in fallbacks:
            job.result = self.run_batch_job_python(job)

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
                # The cached result carries no SoC series, so a job wanting window-end SoCs can only use
                # a hit whose SoCs were kept alongside it
                cached_export_end = self.prediction_cache_export_end.get(job.sim_hash) if job.want_export_end_soc else None
                if cached is not None and (not job.want_export_end_soc or cached_export_end is not None):
                    job.result = shape_batch_result(job, cached, 0.0, 0.0, cached_export_end)
                    continue
                # Two identical trials in one fan-out used to be a cache hit for the second one; the
                # batch has to collapse them itself or that hit is lost (~44% of calls are hits). Keyed on
                # want_export_end_soc too, because the copied result carries the window-end SoCs or not
                duplicate_key = (job.sim_hash, job.want_export_end_soc)
                first = first_by_hash.get(duplicate_key)
                if first is not None:
                    duplicates.append((job, first))
                    continue
                first_by_hash[duplicate_key] = job
            kernel_jobs.append(job)

        if kernel_jobs:
            # A floored scenario (EXPORT_FLOOR_SCENARIOS) is simulated with each target export floored at
            # where the same plan's nominal run ends. The floor comes from the memo when that nominal run
            # is already known, otherwise from its nominal twin in this batch - usually already queued by
            # the same fan-out, else added here with its result discarded - which the kernel runs a phase
            # earlier and floors from directly.
            nominal_by_key = None
            extra = []

            def nominal_key(job):
                """The cache key of this job's trial in the nominal scenario, reusing its base hash"""
                if job.base_hash is None:
                    job.base_hash = prediction_cache_base(job.charge_limit, job.charge_window, job.export_limits, job.export_window)
                return prediction_cache_combine(job.base_hash, PV_SCENARIO_NOMINAL, job.end_record, job.step)

            for job in kernel_jobs:
                if job.pv_scenario not in EXPORT_FLOOR_SCENARIOS or not job.export_window:
                    continue
                key = nominal_key(job)
                known = self.prediction_cache_export_end.get(key)
                if known is not None:
                    job.export_floor_soc = known
                    continue
                if nominal_by_key is None:
                    nominal_by_key = {}
                    for other in kernel_jobs:
                        if other.pv_scenario == PV_SCENARIO_NOMINAL:
                            nominal_by_key.setdefault(nominal_key(other), other)
                twin = nominal_by_key.get(key)
                if twin is None:
                    twin = BatchJob(job.charge_limit, job.charge_window, job.export_window, job.export_limits, PV_SCENARIO_NOMINAL, job.end_record, job.step)
                    nominal_by_key[key] = twin
                    extra.append(twin)
                twin.need_export_end_soc = True
                twin.export_end_key = key
                job.floor_twin = twin
            self.run_kernel_jobs(kernel_jobs + extra, cache)

        for job, first in duplicates:
            job.result = first.result
