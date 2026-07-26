# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's subprocess job control."""

import asyncio
import os
import sys

from annual_job import AnnualJob

STUB = os.path.join(os.path.dirname(__file__), "annual_stub.py")


def stub_command(mode):
    """Return the argv for the stub child in the given mode."""
    return [sys.executable, STUB, mode]


async def run_to_completion(job, mode, timeout=20):
    """Start the stub in the given mode and wait for the job to leave 'running'."""
    started = await job.start(stub_command(mode))
    waited = 0.0
    while job.state == "running" and waited < timeout:
        await asyncio.sleep(0.1)
        waited += 0.1
    return started


def test_annual_job(my_predbat):
    """Verify progress parsing, completion, failure, cancellation and refusal to double-run."""
    failed = False
    print("**** Testing annual_job ****")
    messages = []

    print("Test: a successful run parses progress and returns the results document")
    job = AnnualJob(log=messages.append)
    started = asyncio.run(run_to_completion(job, "ok"))
    if not started:
        print("  ERROR: start() should return True for a fresh job")
        failed = True
    if job.state != "complete":
        print("  ERROR: expected state 'complete', got {} ({})".format(job.state, job.status().get("error")))
        failed = True
    if job.status().get("completed") != 3 or job.status().get("total") != 3:
        print("  ERROR: final progress should be 3/3, got {}".format(job.status()))
        failed = True
    if (job.results or {}).get("year") != 2025:
        print("  ERROR: the results document should be parsed from stdout, got {}".format(job.results))
        failed = True

    print("Test: a malformed progress line does not crash the parser")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "garbage_progress"))
    if job.state != "complete":
        print("  ERROR: a garbage progress line should not fail the run, got {}".format(job.state))
        failed = True
    if job.status().get("message") != "recovered":
        print("  ERROR: parsing should recover after a bad line, got {}".format(job.status()))
        failed = True

    print("Test: a non-zero exit is reported as failed, with the child's stderr kept")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "fail"))
    if job.state != "failed":
        print("  ERROR: expected state 'failed', got {}".format(job.state))
        failed = True
    error_text = job.status().get("error") or ""
    if "something went wrong" not in error_text:
        print("  ERROR: the child's stderr should be reported, got {!r}".format(error_text))
        failed = True
    if "3" not in error_text:
        print("  ERROR: the exit code should be reported, got {!r}".format(error_text))
        failed = True

    print("Test: unparseable stdout is reported as failed rather than a silent empty result")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "bad_output"))
    if job.state != "failed":
        print("  ERROR: unparseable output should fail the run, got {}".format(job.state))
        failed = True
    if job.results is not None:
        print("  ERROR: no results should be exposed after a parse failure, got {}".format(job.results))
        failed = True

    print("Test: a second start while running is refused, and cancel stops the child")

    async def double_start_then_cancel():
        """Start a hanging child, try to start another, then cancel."""
        job = AnnualJob(log=messages.append)
        first = await job.start(stub_command("hang"))
        await asyncio.sleep(0.5)
        second = await job.start(stub_command("hang"))
        cancelled = await job.cancel()
        waited = 0.0
        while job.state == "running" and waited < 10:
            await asyncio.sleep(0.1)
            waited += 0.1
        return first, second, cancelled, job

    first, second, cancelled, job = asyncio.run(double_start_then_cancel())
    if not first:
        print("  ERROR: the first start should succeed")
        failed = True
    if second:
        print("  ERROR: a second start while running must be refused")
        failed = True
    if not cancelled:
        print("  ERROR: cancel should report that it acted")
        failed = True
    if job.state != "cancelled":
        print("  ERROR: expected state 'cancelled', got {}".format(job.state))
        failed = True

    print("Test: a fresh job reports idle with no results")
    job = AnnualJob(log=messages.append)
    if job.state != "idle" or job.results is not None:
        print("  ERROR: a fresh job should be idle with no results, got {} / {}".format(job.state, job.results))
        failed = True
    if job.status().get("elapsed") != 0:
        print("  ERROR: an idle job should report zero elapsed, got {}".format(job.status()))
        failed = True

    return failed
