# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests that log rotation cannot kill a thread that is logging at the same time.

Rotation runs on the main thread and swaps self.logfile underneath every other thread. It used to
close the old handle before opening the replacement, so a component thread part-way through log()
would write to a closed file and take a ValueError all the way up - killing the thread for good. The
load ML training thread died this way, and because components.is_all_alive() then failed, Predbat
reported itself unhealthy on the dashboard for the rest of the run.

Two guarantees are pinned here: the new handle is published before the old one is closed, and a write
that fails anyway is retried and then swallowed rather than raised.
"""

import asyncio
import os
import tempfile

from hass import Hass


class LogHarness:
    """The logfile plumbing of Hass on its own.

    Borrowing the two functions rather than constructing a Hass keeps this to the code under test - a
    real Hass wants config, secrets, an event loop and a Home Assistant connection, none of which say
    anything about whether a write survives rotation.
    """

    log = Hass.log
    write_log_line = Hass.write_log_line

    def __init__(self, logfile):
        """Start out writing to the given open file."""
        self.logfile = logfile


class ClosedOnceFile:
    """A handle that fails its first write, as one closed by rotation mid-call does.

    This is the race made deterministic. The real thread reads self.logfile, rotation closes it, and
    only then does the write land - a window too narrow to hit on demand but wide enough to have
    killed the ML thread repeatedly in the field.
    """

    def __init__(self, harness, replacement):
        """Fail once, then hand the harness the replacement handle rotation would have published."""
        self.harness = harness
        self.replacement = replacement
        self.writes = 0

    def write(self, message):
        """Raise the first time, exactly as a closed file object does."""
        self.writes += 1
        if self.writes == 1:
            self.harness.logfile = self.replacement
            raise ValueError("I/O operation on closed file.")
        return len(message)

    def flush(self):
        """Nothing buffered."""
        return None

    def tell(self):
        """Well under the rotation threshold."""
        return 0


class OrderingFile:
    """Records whether the replacement handle was already published when this one was closed."""

    def __init__(self, harness, real):
        """Wrap a real file so the log still lands somewhere, and report a size that forces rotation."""
        self.harness = harness
        self.real = real
        self.swapped_before_close = None

    def write(self, message):
        """Pass through."""
        return self.real.write(message)

    def flush(self):
        """Pass through."""
        return self.real.flush()

    def tell(self):
        """Always over the 10MB rotation threshold."""
        return 10000001

    def close(self):
        """Note what self.logfile pointed at by the time rotation got round to closing this."""
        self.swapped_before_close = self.harness.logfile is not self
        self.real.close()


def test_write_survives_a_closed_handle():
    """A write against a permanently closed file must not raise."""
    print("  - test_write_survives_a_closed_handle")
    failed = False
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "predbat.log")
        handle = open(path, "w")
        handle.close()
        harness = LogHarness(handle)
        try:
            harness.log("this has nowhere to go")
        except Exception as e:
            print("ERROR: logging to a closed handle raised {}: {}".format(type(e).__name__, e))
            failed = True
    return failed


def test_write_retries_onto_the_new_handle():
    """The retry picks up the handle rotation has just published, so the line is not lost."""
    print("  - test_write_retries_onto_the_new_handle")
    failed = False
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "predbat.log")
        with open(path, "w") as replacement:
            harness = LogHarness(None)
            failing = ClosedOnceFile(harness, replacement)
            harness.logfile = failing

            try:
                harness.log("survived the swap")
            except Exception as e:
                print("ERROR: a mid-rotation write raised {}: {}".format(type(e).__name__, e))
                return True

            if failing.writes != 1:
                print("ERROR: expected exactly one attempt against the dead handle, got {}".format(failing.writes))
                failed = True

        with open(path) as check:
            contents = check.read()
        if "survived the swap" not in contents:
            print("ERROR: the retried line never reached the new logfile, got {}".format(repr(contents)))
            failed = True
    return failed


def test_rotation_publishes_before_closing():
    """Rotation swaps self.logfile before closing the old handle, not after.

    Closing first is what left other threads writing into a dead file. Reversing the order does not
    remove the race on its own - the retry above covers what is left - but it narrows it to the gap
    between one thread's read and its write, rather than the whole of the rename-and-reopen.
    """
    print("  - test_rotation_publishes_before_closing")
    failed = False
    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        try:
            # Rotation works on relative paths, so it has to run somewhere disposable
            os.chdir(root)
            with open("predbat.log", "w") as real:
                harness = LogHarness(None)
                tracked = OrderingFile(harness, real)
                harness.logfile = tracked

                harness.log("the line that trips rotation")

                if tracked.swapped_before_close is None:
                    print("ERROR: rotation did not run, so nothing was closed")
                    return True
                if not tracked.swapped_before_close:
                    print("ERROR: the old handle was closed before the replacement was published")
                    failed = True
                if harness.logfile is tracked:
                    print("ERROR: rotation left self.logfile pointing at the old handle")
                    failed = True

                if not os.path.isfile("predbat.1.log"):
                    print("ERROR: rotation did not produce predbat.1.log")
                    failed = True
                if not os.path.isfile("predbat.log"):
                    print("ERROR: rotation did not reopen predbat.log")
                    failed = True

                # The replacement must be usable, which is the whole point of reopening it
                try:
                    harness.log("after rotation")
                except Exception as e:
                    print("ERROR: logging after rotation raised {}: {}".format(type(e).__name__, e))
                    failed = True
                harness.logfile.close()

            with open("predbat.1.log") as check:
                rotated = check.read()
            if "the line that trips rotation" not in rotated:
                print("ERROR: the pre-rotation log was not carried into predbat.1.log, got {}".format(repr(rotated)))
                failed = True
        finally:
            os.chdir(previous_cwd)
    return failed


class StopHarness:
    """The shutdown path of Hass, with terminate() and the thread list stubbed out."""

    log = Hass.log
    write_log_line = Hass.write_log_line
    stop_all = Hass.stop_all

    def __init__(self, logfile, threads):
        """Hold the handle and the threads stop_all() will join."""
        self.logfile = logfile
        self.threads = threads

    async def terminate(self):
        """Nothing to tear down in the test."""
        return None


class FakeThread:
    """A thread that reports itself alive or not, and records that join() was called."""

    def __init__(self, alive, name="worker"):
        """alive is what is_alive() reports after the join returns."""
        self._alive = alive
        self.name = name
        self.joined = False

    def join(self, timeout=None):
        """Record the join; a thread that outlives its timeout stays alive."""
        self.joined = True

    def is_alive(self):
        """Report whether this thread outlived its join."""
        return self._alive


def test_shutdown_leaves_logfile_open_for_surviving_threads():
    """stop_all() must not close the logfile while a thread that logs is still running.

    The join gives up after five minutes and an ML training run is tens of minutes, so a thread
    routinely outlives it. Closing underneath one leaves it writing to a closed handle for the rest
    of its life - observed in the field as every training epoch going to stderr with
    "unable to write to the Predbat logfile (I/O operation on closed file.)".
    """
    print("  - test_shutdown_leaves_logfile_open_for_surviving_threads")
    failed = False
    with tempfile.TemporaryDirectory() as root:
        # A thread still running: the handle must stay usable
        handle = open(os.path.join(root, "a.log"), "w")
        harness = StopHarness(handle, [FakeThread(alive=True, name="load_ml")])
        asyncio.run(harness.stop_all())
        if handle.closed:
            print("ERROR: logfile was closed while a thread was still alive")
            failed = True
        else:
            try:
                harness.log("a surviving thread can still log")
            except Exception as e:
                print("ERROR: logging after stop_all raised {}".format(e))
                failed = True
            handle.close()

        # Everything finished: closing is correct and still happens
        handle2 = open(os.path.join(root, "b.log"), "w")
        harness2 = StopHarness(handle2, [FakeThread(alive=False)])
        asyncio.run(harness2.stop_all())
        if not handle2.closed:
            print("ERROR: logfile should be closed once every thread has finished")
            failed = True

        # No threads at all - the ordinary case
        handle3 = open(os.path.join(root, "c.log"), "w")
        harness3 = StopHarness(handle3, [])
        asyncio.run(harness3.stop_all())
        if not handle3.closed:
            print("ERROR: logfile should be closed when there are no threads")
            failed = True

    return failed


def run_log_rotation_tests(my_predbat):
    """Run every log rotation test."""
    print("**** Running log rotation tests ****\n")
    failed = test_write_survives_a_closed_handle()
    failed |= test_write_retries_onto_the_new_handle()
    failed |= test_rotation_publishes_before_closing()
    failed |= test_shutdown_leaves_logfile_open_for_surviving_threads()
    return failed
