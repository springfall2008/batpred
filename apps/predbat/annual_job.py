# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Subprocess control for the Annual prediction run.

The annual engine is one to three minutes of synchronous CPU work - two to six
with a car - so running it inside the web server's event loop would freeze the
whole Predbat interface and the five minute optimiser loop that shares it. It
runs as a child process instead, handing progress back on stderr and the results
document on stdout.

This module owns the process and nothing else: no HTML, no Storage, no opinion
about what the results mean. That is what lets it be tested against a stub child
rather than the real engine.
"""

import asyncio
import json
import time

# How much of the child's stderr to keep for the failure message. Enough to carry
# a traceback, bounded so a chatty failure cannot grow without limit.
MAX_ERROR_LINES = 20

# Grace period between asking the child to stop and killing it outright
CANCEL_GRACE_SECONDS = 5.0


class AnnualJob:
    """Runs one annual prediction child process at a time and tracks its progress."""

    def __init__(self, log):
        """Create an idle job that logs through the supplied callable."""
        self.log = log
        self.state = "idle"
        self.completed = 0
        self.total = 0
        self.message = ""
        self.error = None
        self.results = None
        self.started_at = None
        self.finished_at = None
        self._process = None
        self._task = None
        self._stderr_tail = []
        # Bumped synchronously - with no `await` in between - the instant a new
        # run is accepted. A supervisor captures the value current at its own
        # spawn and must not touch shared state once it no longer matches: see
        # the comment in _supervise for why comparing `self._process` alone is
        # not enough.
        self._generation = 0

    def status(self):
        """Return a JSON-serialisable snapshot for the polling endpoint."""
        elapsed = 0
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            elapsed = int(end - self.started_at)
        return {
            "state": self.state,
            "completed": self.completed,
            "total": self.total,
            "message": self.message,
            "elapsed": elapsed,
            "error": self.error,
        }

    async def start(self, command):
        """Spawn the child. Returns False when a run is already in progress.

        Refusing rather than queueing is deliberate: two annual runs on the same
        machine would compete for the same CPU and both would take twice as long.
        """
        if self.state == "running":
            self.log("Warn: Annual: a run is already in progress, refusing to start another")
            return False

        # Bumped first, synchronously, before anything else: this is what a
        # stale supervisor checks to know it has been superseded. Setting it
        # here - rather than after the subprocess exists - closes the window
        # where `self.state` already says "running" for the new run but
        # `self._process` has not been reassigned yet, which would otherwise
        # let an old supervisor's identity check pass by accident.
        self._generation += 1
        generation = self._generation

        self.state = "running"
        self.completed = 0
        self.total = 0
        self.message = "Starting"
        self.error = None
        self.results = None
        self.started_at = time.time()
        self.finished_at = None
        self._stderr_tail = []

        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as exception:
            if self._generation == generation:
                self.state = "failed"
                self.finished_at = time.time()
                self.error = "Could not start the annual run: {}".format(exception)
                self.log("Warn: Annual: {}".format(self.error))
            return False

        self._process = process
        self._task = asyncio.create_task(self._supervise(generation, process))
        self._task.add_done_callback(self._on_task_done)
        return True

    async def cancel(self):
        """Ask the child to stop, killing it if it does not. Returns False if nothing was running."""
        if self.state != "running" or self._process is None:
            return False
        process = self._process
        self.state = "cancelled"
        self.finished_at = time.time()
        self.message = "Cancelled"
        try:
            process.terminate()
        except ProcessLookupError:
            return True
        try:
            await asyncio.wait_for(process.wait(), timeout=CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            self.log("Warn: Annual: the run did not stop when asked, killing it")
            try:
                process.kill()
            except ProcessLookupError:
                pass
            else:
                # Reap it - without this, cancel() could return before the
                # child is actually gone, leaving returncode unset.
                await process.wait()
        return True

    def _on_task_done(self, task):
        """Log if the supervisor task ended by raising rather than settling a terminal state itself.

        Every expected outcome inside `_supervise` is caught and turned into a
        terminal state there. This callback is the fallback for anything that
        still escapes - otherwise the job would be stuck reporting 'running'
        forever, refusing every further start().
        """
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self.log("Warn: Annual: the supervisor task ended unexpectedly: {}".format(exception))

    async def _read_progress(self, generation, stream):
        """Consume the child's stderr, updating progress and keeping a tail for errors.

        `generation` is the run this call was started for, captured by the
        caller at spawn time. If a later run has since been accepted - because
        cancel() returned before this stream had drained, and a fresh start()
        followed straight after - the line is still consumed so the pipe keeps
        draining, but it must not be allowed to overwrite the newer run's
        progress or error tail.
        """
        while True:
            line = await stream.readline()
            if not line:
                break
            if self._generation != generation:
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            self._stderr_tail.append(text)
            if len(self._stderr_tail) > MAX_ERROR_LINES:
                self._stderr_tail.pop(0)
            try:
                record = json.loads(text)
            except ValueError:
                # Not a progress record - the child is allowed to write plain
                # warnings to stderr, and one bad line must not stop the parse.
                continue
            if isinstance(record, dict) and "completed" in record:
                self.completed = record.get("completed", self.completed)
                self.total = record.get("total", self.total)
                self.message = record.get("message", self.message)

    async def _supervise(self, generation, process):
        """Read both streams to completion for this child, then settle the final state.

        `generation` is captured by the caller at the instant this run was
        accepted, before the child even existed. Comparing `self._process` to
        this call's `process` is not sufficient on its own: `start()` sets
        `self.state = "running"` before it awaits the subprocess's creation,
        so there is a window where a new run is already "running" but
        `self._process` still points at the previous child. A stale supervisor
        resuming inside that window would see its own `process` still matching
        `self._process`, and `self.state` no longer "cancelled", and wrongly
        conclude it was safe to report its own child's outcome. The generation
        counter has no such window - it is bumped synchronously as the very
        first thing `start()` does - so it is the only reliable guard.
        """
        try:
            stdout_data, _ = await asyncio.gather(process.stdout.read(), self._read_progress(generation, process.stderr))
            await process.wait()
        except Exception as exception:  # noqa: BLE001 - a supervisor must never die silently, see _on_task_done
            if self._generation == generation:
                self.state = "failed"
                self.finished_at = time.time()
                self.error = "The annual run could not be read: {}".format(exception)
                self.log("Warn: Annual: {}".format(self.error))
            return

        if self._generation != generation:
            # Superseded by a later run - its own supervisor owns state now.
            return

        if self.state == "cancelled":
            return

        tail = "\n".join(self._stderr_tail)

        if process.returncode != 0:
            self.state = "failed"
            self.finished_at = time.time()
            self.error = "The annual run exited with code {}.\n{}".format(process.returncode, tail)
            self.log("Warn: Annual: {}".format(self.error))
            return

        try:
            results = json.loads(stdout_data.decode("utf-8", errors="replace"))
            if not isinstance(results, dict):
                raise ValueError("the results document was not a JSON object, got {}".format(type(results).__name__))
        except ValueError as exception:
            # A zero exit with unreadable output is worse than a crash: it would
            # otherwise render as an empty result that looks like a real answer.
            self.state = "failed"
            self.finished_at = time.time()
            self.results = None
            self.error = "The annual run finished but its output could not be read: {}\n{}".format(exception, tail)
            self.log("Warn: Annual: {}".format(self.error))
            return

        self.results = results
        self.state = "complete"
        self.finished_at = time.time()
        self.message = "Complete"
        if self.total:
            self.completed = self.total
