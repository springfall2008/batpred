# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's subprocess job control."""

import asyncio
import os
import sys
import time

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
    elapsed_first = job.status().get("elapsed")
    # elapsed is int()-truncated to whole seconds, so a sleep shorter than a second
    # (0.2s, say) usually lands inside the same truncated second and would pass this
    # assertion even if the freeze were removed and elapsed kept advancing with wall
    # time - discriminating only by luck, on whichever side of a second boundary the
    # first read happened to fall. Sleeping past a full second guarantees at least
    # one more second has elapsed on the wall clock than before, so a still-advancing
    # elapsed is caught deterministically rather than only sometimes.
    time.sleep(1.1)
    elapsed_second = job.status().get("elapsed")
    if elapsed_first != elapsed_second:
        print("  ERROR: elapsed should freeze once a run is complete, got {} then {}".format(elapsed_first, elapsed_second))
        failed = True

    print("Test: a malformed progress line does not crash the parser")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "garbage_progress"))
    if job.state != "complete":
        print("  ERROR: a garbage progress line should not fail the run, got {}".format(job.state))
        failed = True
    # The proof that the parser recovered is the line *after* the garbage one
    # being applied - completed/total went from 0/0 to 1/1. Asserting on the
    # terminal message instead would test message policy, not the parser, and
    # a correct implementation is free to replace the last progress message
    # with a generic "Complete" once the run finishes.
    if job.status().get("completed") != 1 or job.status().get("total") != 1:
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
    if "exited with code 3" not in error_text:
        print("  ERROR: the exit code should be reported precisely, got {!r}".format(error_text))
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

    print("Test: valid JSON that is not an object is reported as failed, not a silent empty result")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "null_output"))
    if job.state != "failed":
        print("  ERROR: a non-object results document should fail the run, got {}".format(job.state))
        failed = True
    if job.results is not None:
        print("  ERROR: no results should be exposed after a non-object parse, got {}".format(job.results))
        failed = True

    print("Test: streams bigger than a pipe buffer on both stdout and stderr do not deadlock")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "big_streams", timeout=30))
    if job.state != "complete":
        print("  ERROR: a large-output run should still complete, got {} ({})".format(job.state, job.status().get("error")))
        failed = True
    if (job.results or {}).get("year") != 2025:
        print("  ERROR: the large results document should still be parsed, got keys {}".format(list((job.results or {}).keys())))
        failed = True

    print("Test: a second start while running is refused, and cancel stops and reaps the child")

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
    # Proof that the child was actually reaped, not just that the state string
    # was set: `state` is assigned synchronously before terminate() is even
    # called, so checking `state` alone would pass even if cancel() never
    # touched the process.
    if job._process is None or job._process.returncode is None:
        print("  ERROR: cancel should have reaped the child, but returncode is {}".format(job._process and job._process.returncode))
        failed = True

    print("Test: a run started immediately after cancelling is not clobbered by the old supervisor")

    async def cancel_then_restart_immediately():
        """Cancel a hanging child, then start a fresh run before the old supervisor has drained its pipes."""
        job = AnnualJob(log=messages.append)
        await job.start(stub_command("hang"))
        await asyncio.sleep(0.2)
        await job.cancel()
        # No delay here: the old supervisor task is still scheduled to run and
        # may not have observed the cancellation yet - that overlap is exactly
        # what let a stale supervisor stamp its state over a new run.
        started = await job.start(stub_command("ok"))
        waited = 0.0
        while job.state == "running" and waited < 20:
            await asyncio.sleep(0.1)
            waited += 0.1
        return started, job

    restart_started, restart_job = asyncio.run(cancel_then_restart_immediately())
    if not restart_started:
        print("  ERROR: starting again immediately after cancel should succeed")
        failed = True
    if restart_job.state != "complete":
        print("  ERROR: the new run should complete cleanly, got {} ({})".format(restart_job.state, restart_job.status().get("error")))
        failed = True
    if (restart_job.results or {}).get("year") != 2025:
        print("  ERROR: the new run's results must not be clobbered by the superseded supervisor, got {}".format(restart_job.results))
        failed = True

    print("Test: cancelling an already-cancelled job is a no-op that reports nothing to act on")
    second_cancel = asyncio.run(job.cancel())
    if second_cancel:
        print("  ERROR: cancelling a job that is not running should return False, got {}".format(second_cancel))
        failed = True

    print("Test: cancelling an idle job reports nothing to act on")
    idle_job = AnnualJob(log=messages.append)
    idle_cancel = asyncio.run(idle_job.cancel())
    if idle_cancel:
        print("  ERROR: cancelling an idle job should return False, got {}".format(idle_cancel))
        failed = True

    print("Test: a bad command is a start failure, not a silent no-op")

    async def failed_start():
        """Try to start a command that cannot be executed at all."""
        job = AnnualJob(log=messages.append)
        started = await job.start(["/nonexistent/predbat-annual-stub-does-not-exist"])
        return job, started

    bad_job, bad_started = asyncio.run(failed_start())
    if bad_started:
        print("  ERROR: starting a non-existent command should return False")
        failed = True
    if bad_job.state != "failed":
        print("  ERROR: a start failure should leave the job in state 'failed', got {}".format(bad_job.state))
        failed = True
    if not any("Could not start the annual run" in message for message in messages):
        print("  ERROR: a start failure should be logged, got {}".format(messages))
        failed = True

    print("Test: a fresh job reports idle with no results")
    job = AnnualJob(log=messages.append)
    if job.state != "idle" or job.results is not None:
        print("  ERROR: a fresh job should be idle with no results, got {} / {}".format(job.state, job.results))
        failed = True
    if job.status().get("elapsed") != 0:
        print("  ERROR: an idle job should report zero elapsed, got {}".format(job.status()))
        failed = True

    print("Test: a job that has already completed can be started again")
    reused_job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(reused_job, "ok"))
    if reused_job.state != "complete":
        print("  ERROR: the first run on the reused job should complete, got {}".format(reused_job.state))
        failed = True
    second_started = asyncio.run(run_to_completion(reused_job, "ok"))
    if not second_started:
        print("  ERROR: starting again after completion should succeed")
        failed = True
    if reused_job.state != "complete":
        print("  ERROR: the second run should also complete, got {}".format(reused_job.state))
        failed = True

    return failed
