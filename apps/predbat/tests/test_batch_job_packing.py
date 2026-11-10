# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for packing the kernel batch's PkBatchJob records with struct (run_prediction_kernel_batch).

The batch used to set each job's seventeen fields one at a time through ctypes; it now packs each job
with struct and copies the whole batch into the job array at once, falling back to the per-field fill
when the layouts disagree. These tests pin that:

- the layout check holds on this platform, so the fast path is the one that ships
- a full plan replay hands the kernel exactly the same jobs either way: every field, every buffer the
  jobs point at, and which jobs share a buffer (a floored job must point at its twin's output)
- the kernel's results, and the plan, are identical either way
- the buffers a packed job points at stay alive for the whole kernel call: a packed job holds only
  their addresses, so ctypes no longer keeps them alive through the job array
"""

import ctypes
import gc

import prediction_kernel
from tests.test_infra import reset_inverter
from tests.test_kernel_parity import kernel_available

# How often a packed replay frees and reuses memory before describing a batch's jobs
CHURN_EVERY = 10


def test_layout_check_holds():
    """The struct format matches PkBatchJob on this platform, so the packed path is the one in use"""
    print("**** test_layout_check_holds ****")
    failed = False
    if not prediction_kernel._BATCH_JOB_STRUCT_USABLE:
        print("ERROR: batch job packing was disabled on this platform - PkBatchJob and '{}' disagree on layout".format(prediction_kernel._BATCH_JOB_STRUCT.format))
        failed = True
    if len(prediction_kernel._BATCH_JOB_CODES) != len(prediction_kernel.PkBatchJob._fields_):
        print("ERROR: the job format has {} codes for {} fields".format(len(prediction_kernel._BATCH_JOB_CODES), len(prediction_kernel.PkBatchJob._fields_)))
        failed = True
    if prediction_kernel._BATCH_JOB_STRUCT.size != ctypes.sizeof(prediction_kernel.PkBatchJob):
        print("ERROR: the job format packs {} bytes but PkBatchJob is {}".format(prediction_kernel._BATCH_JOB_STRUCT.size, ctypes.sizeof(prediction_kernel.PkBatchJob)))
        failed = True
    if not failed:
        print("PASS")
    return failed


def pointer_address(pointer):
    """The address a ctypes pointer field holds, or 0 for NULL"""
    return ctypes.cast(pointer, ctypes.c_void_p).value or 0


def decode_batch(job_array, n_jobs):
    """Describe every job of a batch by value: its scalar fields, the contents of the buffers it points at, and which jobs share an output buffer.

    Addresses differ between two runs, so a pointer is described by what it points at instead. The
    window-end SoC buffers are outputs, so they are described by which job owns them, and a floor that
    points at one is described as that job's index - the twin relationship the kernel relies on.
    """
    owner = {}
    for index in range(n_jobs):
        address = pointer_address(job_array[index].export_end_soc_out)
        if address:
            owner[address] = index
    jobs = []
    for index in range(n_jobs):
        job = job_array[index]
        n_charge, n_export = job.n_charge, job.n_export
        floor_address = pointer_address(job.export_floor_soc)
        if not floor_address:
            floor = None
        elif floor_address in owner:
            floor = ("twin", owner[floor_address])
        else:
            floor = tuple(job.export_floor_soc[:n_export])
        jobs.append(
            (
                tuple(job.charge_limit[:n_charge]),
                tuple(job.charge_start[:n_charge]),
                tuple(job.charge_end[:n_charge]),
                tuple((limit.mode, limit.target, limit.power) for limit in job.export_limits[:n_export]),
                tuple(job.export_start[:n_export]),
                tuple(job.export_end[:n_export]),
                pointer_address(job.soc_out),
                bool(pointer_address(job.export_end_soc_out)),
                floor,
                n_charge,
                n_export,
                job.pv_scenario,
                job.end_record,
                job.step,
                job.soc_range_start_step,
                job.soc_range_end_step,
                job.phase,
            )
        )
    return jobs


def decode_results(job_array, result_array, n_jobs):
    """Describe every job's kernel result, including the window-end SoCs it wrote"""
    results = []
    for index in range(n_jobs):
        result = result_array[index]
        job = job_array[index]
        end_socs = tuple(job.export_end_soc_out[: job.n_export]) if pointer_address(job.export_end_soc_out) else None
        results.append((result.status, result.result.final_metric, result.result.final_soc, result.result.soc_min, result.soc_range_min, result.soc_range_max, end_socs))
    return results


def churn_memory():
    """Free what can be freed and reuse the memory, so a buffer kept alive by nothing would be overwritten"""
    gc.collect()
    return [(ctypes.c_double * size)(*([-12345.0] * size)) for size in (1, 2, 4, 8, 16, 32, 48, 64, 96, 128) for _ in range(20)]


def replay(my_predbat, dump, packed):
    """Replay a plan with batch job packing on or off, returning (batches, plan) as described by value"""
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(dump)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()
    my_predbat.args["threads"] = 0
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = True
    my_predbat.plan_valid = False

    lib = prediction_kernel.KERNEL_LIB
    real_run_batch = lib.pk_run_batch
    batches = []

    def recording(handle, job_array, n_jobs, result_array, n_threads):
        """Describe the jobs, after reusing any memory nothing holds, then run them and describe the results"""
        # A full collection per batch would make the group several times slower; every tenth batch still
        # covers hundreds of batches of every kind across the two replays
        garbage = churn_memory() if packed and len(batches) % CHURN_EVERY == 0 else None
        jobs = decode_batch(job_array, n_jobs)
        return_code = real_run_batch(handle, job_array, n_jobs, result_array, n_threads)
        batches.append((jobs, decode_results(job_array, result_array, n_jobs) if return_code == 0 else return_code))
        del garbage
        return return_code

    saved_usable = prediction_kernel._BATCH_JOB_STRUCT_USABLE
    prediction_kernel._BATCH_JOB_STRUCT_USABLE = packed
    lib.pk_run_batch = recording
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        lib.pk_run_batch = real_run_batch
        prediction_kernel._BATCH_JOB_STRUCT_USABLE = saved_usable
        my_predbat.prediction_kernel_enable = False
    plan = (my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best)
    return batches, repr(plan)


def test_replay_packed_matches_fields(my_predbat, dump):
    """A full plan hands the kernel the same jobs, gets the same results and makes the same plan, packed or filled field by field"""
    print("**** test_replay_packed_matches_fields {} ****".format(dump))
    failed = False
    filled_batches, filled_plan = replay(my_predbat, dump, packed=False)
    packed_batches, packed_plan = replay(my_predbat, dump, packed=True)

    if len(filled_batches) != len(packed_batches):
        print("ERROR: {} batches filled field by field but {} packed".format(len(filled_batches), len(packed_batches)))
        failed = True
    for number, (filled, packed) in enumerate(zip(filled_batches, packed_batches)):
        if filled[0] != packed[0]:
            first = next(index for index, (a, b) in enumerate(zip(filled[0], packed[0])) if a != b) if len(filled[0]) == len(packed[0]) else None
            print("ERROR: batch {} jobs differ (first differing job {})".format(number, first))
            failed = True
            break
        if filled[1] != packed[1]:
            print("ERROR: batch {} kernel results differ".format(number))
            failed = True
            break
    if filled_plan != packed_plan:
        print("ERROR: the plan differs between packed and per-field jobs")
        failed = True

    # The replay has to exercise every kind of job, or it proves little about the fields that kind sets
    all_jobs = [job for jobs, _ in packed_batches for job in jobs]
    twins = sum(1 for job in all_jobs if job[16] == 1)
    floors = sum(1 for job in all_jobs if job[8] is not None and job[8][0] != "twin")
    end_socs = sum(1 for job in all_jobs if job[7])
    if len(all_jobs) < 1000:
        print("ERROR: the replay only ran {} batched jobs".format(len(all_jobs)))
        failed = True
    if not failed:
        print("PASS ({} batches, {} jobs: {} floored from a twin, {} with a memo floor, {} returning window-end SoCs)".format(len(packed_batches), len(all_jobs), twins, floors, end_socs))
    return failed, twins, floors, end_socs


def run_batch_job_packing_tests(my_predbat):
    """Run the batch job packing tests, returns True on failure.

    my_predbat should be a fresh instance: the replays load debug dumps over it.
    """
    available, required_failure = kernel_available()
    if required_failure:
        return True
    failed = test_layout_check_holds()
    if not available:
        print("WARNING: Prediction kernel not available - batch job packing replays SKIPPED")
        return failed
    totals = [0, 0, 0]
    for dump in ("cases/predbat_debug_agile1.yaml", "cases/predbat_debug_pre_saving1.yaml"):
        dump_failed, *counts = test_replay_packed_matches_fields(my_predbat, dump)
        failed |= dump_failed
        totals = [total + count for total, count in zip(totals, counts)]
    # Between them the replays must reach every optional field: a twin floor, a memo floor and a window-end SoC buffer
    for name, count in zip(("jobs floored from a twin", "jobs with a memo floor", "jobs returning window-end SoCs"), totals):
        if count == 0:
            print("ERROR: no replay ran any {}, so packing those fields is untested".format(name))
            failed = True
    return failed
