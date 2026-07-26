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
        self._process = None
        self._stderr_tail = []

    def status(self):
        """Return a JSON-serialisable snapshot for the polling endpoint."""
        elapsed = 0
        if self.started_at is not None:
            end = self.started_at if self.state == "idle" else time.time()
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

        self.state = "running"
        self.completed = 0
        self.total = 0
        self.message = "Starting"
        self.error = None
        self.results = None
        self.started_at = time.time()
        self._stderr_tail = []

        try:
            self._process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as exception:
            self.state = "failed"
            self.error = "Could not start the annual run: {}".format(exception)
            self.log("Warn: Annual: {}".format(self.error))
            return False

        asyncio.ensure_future(self._supervise())
        return True

    async def cancel(self):
        """Ask the child to stop, killing it if it does not. Returns False if nothing was running."""
        if self.state != "running" or self._process is None:
            return False
        self.state = "cancelled"
        self.message = "Cancelled"
        try:
            self._process.terminate()
        except ProcessLookupError:
            return True
        try:
            await asyncio.wait_for(self._process.wait(), timeout=CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            self.log("Warn: Annual: the run did not stop when asked, killing it")
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
        return True

    async def _read_progress(self, stream):
        """Consume the child's stderr, updating progress and keeping a tail for errors."""
        while True:
            line = await stream.readline()
            if not line:
                break
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

    async def _supervise(self):
        """Read both streams to completion, then settle the final state."""
        process = self._process
        try:
            stdout_data, _ = await asyncio.gather(process.stdout.read(), self._read_progress(process.stderr))
            await process.wait()
        except (OSError, ValueError) as exception:
            self.state = "failed"
            self.error = "The annual run could not be read: {}".format(exception)
            self.log("Warn: Annual: {}".format(self.error))
            return

        if self.state == "cancelled":
            return

        tail = "\n".join(self._stderr_tail)

        if process.returncode != 0:
            self.state = "failed"
            self.error = "The annual run exited with code {}.\n{}".format(process.returncode, tail)
            self.log("Warn: Annual: {}".format(self.error))
            return

        try:
            self.results = json.loads(stdout_data.decode("utf-8", errors="replace"))
        except ValueError as exception:
            # A zero exit with unreadable output is worse than a crash: it would
            # otherwise render as an empty result that looks like a real answer.
            self.state = "failed"
            self.results = None
            self.error = "The annual run finished but its output could not be read: {}\n{}".format(exception, tail)
            self.log("Warn: Annual: {}".format(self.error))
            return

        self.state = "complete"
        if not self.message or self.message == "Starting":
            # Only fall back to a generic message when the child never reported
            # one of its own - the last progress message is more informative.
            self.message = "Complete"
        if self.total:
            self.completed = self.total
