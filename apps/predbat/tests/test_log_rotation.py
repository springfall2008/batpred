# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for configurable log rotation (#5076) - the log_count apps.yaml setting, the
two-digit predbat.NN.log naming, and the migration path that reads the single-digit names an
older Predbat wrote.
"""

import asyncio
import builtins
import os
import tempfile
import threading

from hass import Hass
from utils import (
    PREDBAT_LOG_COUNT_DEFAULT,
    PREDBAT_LOG_COUNT_MAX,
    PREDBAT_LOG_COUNT_MIN,
    predbat_log_count,
    predbat_log_file_prev,
    predbat_log_name,
    predbat_log_name_legacy,
    read_predbat_log,
    rotate_predbat_logs,
)


def _touch(name, content=""):
    """Create a file with known content, for asserting which file ended up where."""
    with open(name, "w", encoding="utf-8") as handle:
        handle.write(content)


def _rotate(args):
    """
    Run the rotation exactly as Hass.log() does, against the current working directory.

    Calls the same rotate_predbat_logs() Hass.log() calls, rather than a hand-copy of its loop -
    a copy can diverge from the code it is meant to be testing and still pass, which is exactly
    what let the off-by-one Copilot found on #5076 ship with a green test suite (the copy had the
    same bug, so it agreed with itself). Only the parts of Hass.log() this file does not otherwise
    exercise - closing/reopening the live predbat.log - are still done directly here.
    """
    max_logs = predbat_log_count(args) - 1
    rotate_predbat_logs(max_logs)
    os.rename("predbat.log", predbat_log_name(1))
    _touch("predbat.log", "live")


class RedactionFields:
    """The redaction state log() needs, for harnesses that borrow Hass.log directly.

    log() redacts secrets on the way to write_log_line() (#4770) and reads its compiled pattern
    from these fields. Borrowing the real methods rather than stubbing redaction away keeps the
    harnesses exercising the same log() path Predbat runs, so a future change there cannot pass
    these tests while breaking the real one.
    """

    _LOG_SECRET_PATTERN_UNSET = Hass._LOG_SECRET_PATTERN_UNSET
    _log_secret_fingerprint = Hass._log_secret_fingerprint
    _log_secret_pattern = Hass._log_secret_pattern

    def init_redaction_fields(self):
        """Set up an empty secret set, so redaction compiles to "nothing to redact"."""
        self.secrets = {}
        self._log_secret_pattern_lock = threading.Lock()
        self._log_secret_pattern_cache = self._LOG_SECRET_PATTERN_UNSET
        self._log_secret_pattern_fingerprint = None


class LogHarness(RedactionFields):
    """The logfile plumbing of Hass on its own.

    Borrowing the two functions rather than constructing a Hass keeps this to the code under test - a
    real Hass wants config, secrets, an event loop and a Home Assistant connection, none of which say
    anything about whether a write survives rotation.
    """

    log = Hass.log
    write_log_line = Hass.write_log_line

    def __init__(self, logfile):
        """Start out writing to the given open file.

        args is required because log() reads the retained-logfile count from config (#5076), and
        the redaction cache fields because log() builds its pattern from args/secrets (#4770).
        """
        self.logfile = logfile
        self.args = {}
        self.init_redaction_fields()


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

                # The rotated name comes from predbat_log_name() since the retention count became
                # configurable (#5076), so ask rather than assuming "predbat.1.log"
                if not os.path.isfile(predbat_log_name(1)):
                    print("ERROR: rotation did not produce {}".format(predbat_log_name(1)))
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

            with open(predbat_log_name(1)) as check:
                rotated = check.read()
            if "the line that trips rotation" not in rotated:
                print("ERROR: the pre-rotation log was not carried into {}, got {}".format(predbat_log_name(1), repr(rotated)))
                failed = True
        finally:
            os.chdir(previous_cwd)
    return failed


class StopHarness(RedactionFields):
    """The shutdown path of Hass, with terminate() and the thread list stubbed out."""

    log = Hass.log
    write_log_line = Hass.write_log_line
    stop_all = Hass.stop_all

    def __init__(self, logfile, threads):
        """Hold the handle and the threads stop_all() will join."""
        self.logfile = logfile
        self.threads = threads
        self.args = {}
        self.init_redaction_fields()

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


def test_component_threads_are_daemon():
    """create_task() threads must not hold the process open at exit.

    Python joins every non-daemon thread before the interpreter exits, so a component still working
    after stop_all() has given up keeps the whole process alive. Observed on an auto-update restart:
    shutdown completed, then ML training carried on for another ten minutes and began a further
    curriculum pass, with the restart blocked behind it the entire time.

    Asserted on the flag rather than by timing a shutdown, which would be slow and racy.
    """
    print("  - test_component_threads_are_daemon")
    failed = False
    created = {}

    class ThreadHarness:
        """create_task() with the thread class swapped for a fake this test can examine."""

        create_task = Hass.create_task

        def __init__(self):
            """Collect the threads create_task() makes."""
            self.threads = []
            self.args = {}

        def log(self, message, **kwargs):
            """Swallow the creation log line."""
            return None

        def task_waiter(self, task):
            """Never actually run - the fake thread does not start anything."""
            return None

    class FakeThread:
        """Records how it was constructed and does nothing when started."""

        def __init__(self, name=None, target=None, args=None, daemon=False):
            """Capture the daemon flag the caller asked for."""
            created["daemon"] = daemon
            created["name"] = name
            self.name = name

        def start(self):
            """No-op."""
            return None

    import threading as threading_module

    real_thread = threading_module.Thread
    threading_module.Thread = FakeThread
    try:
        harness = ThreadHarness()
        harness.create_task(None, name="load_ml_component_task")
    finally:
        threading_module.Thread = real_thread

    if not created:
        print("ERROR: create_task did not construct a thread, the test harness is wrong")
        return True
    if created.get("daemon") is not True:
        print("ERROR: component threads must be daemon or they block process exit, got daemon={}".format(created.get("daemon")))
        failed = True
    if created.get("name") != "load_ml_component_task":
        print("ERROR: the thread name should be passed through, got {}".format(created.get("name")))
        failed = True
    return failed


def test_rotation_rolls_back_when_the_replacement_cannot_be_opened():
    """A rename that lands but an open that fails must put the file back.

    Raised in review on #4684: if os.rename succeeds and open() then fails - a full disk being the
    realistic case - restoring self.logfile is not enough. The live pathname has already moved, so
    writes keep going to the rotated inode and every later rotation renames a predbat.log that is not
    there. Logging never recovers, even once the disk clears.
    """
    print("  - test_rotation_rolls_back_when_the_replacement_cannot_be_opened")
    failed = False
    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        try:
            os.chdir(root)
            with open("predbat.log", "w") as real:
                harness = LogHarness(None)
                tracked = OrderingFile(harness, real)
                harness.logfile = tracked

                real_open = builtins.open
                calls = {"n": 0}

                def failing_open(*args, **kwargs):
                    """Fail only the rotation's reopen of predbat.log, as a full disk would."""
                    if args and args[0] == "predbat.log" and len(args) > 1 and "w" in str(args[1]):
                        calls["n"] += 1
                        raise OSError(28, "No space left on device")
                    return real_open(*args, **kwargs)

                builtins.open = failing_open
                try:
                    harness.log("the line that trips rotation")
                finally:
                    builtins.open = real_open

                if calls["n"] == 0:
                    print("ERROR: the reopen was never attempted, the test did not exercise rotation")
                    return True
                if not os.path.isfile("predbat.log"):
                    print("ERROR: the rotation was not rolled back - predbat.log is missing, so logging cannot recover")
                    failed = True
                if harness.logfile is not tracked:
                    print("ERROR: the original handle should be kept when the replacement cannot be opened")
                    failed = True
                try:
                    harness.log("still able to log")
                except Exception as e:
                    print("ERROR: logging after a failed rotation raised {}".format(e))
                    failed = True
        finally:
            os.chdir(previous_cwd)
    return failed


def run_log_rotation_tests(my_predbat):
    """
    Test configurable log rotation, two-digit naming and single-digit migration (#5076).
    """
    failed = False
    print("**** Running log_rotation tests ****")

    saved_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            print("Test 1: log_count defaults and clamps")
            for args, expected, why in (
                ({}, PREDBAT_LOG_COUNT_DEFAULT, "absent"),
                ({"log_count": 25}, 25, "in range"),
                ({"log_count": 1}, PREDBAT_LOG_COUNT_MIN, "below minimum"),
                ({"log_count": 500}, PREDBAT_LOG_COUNT_MAX, "above maximum"),
                ({"log_count": "30"}, 30, "numeric string"),
                ({"log_count": "banana"}, PREDBAT_LOG_COUNT_DEFAULT, "non-numeric"),
                ({"log_count": None}, PREDBAT_LOG_COUNT_DEFAULT, "null"),
                # YAML accepts non-finite numeric scalars (.inf/-.inf), which parse as a float
                # int() cannot convert - OverflowError, not caught by the (TypeError, ValueError)
                # this already guarded against (Copilot review on #5076).
                ({"log_count": float("inf")}, PREDBAT_LOG_COUNT_DEFAULT, "positive infinity"),
                ({"log_count": float("-inf")}, PREDBAT_LOG_COUNT_DEFAULT, "negative infinity"),
            ):
                actual = predbat_log_count(args)
                if actual != expected:
                    print("  ERROR: log_count {} ({}) should give {}, got {}".format(args, why, expected, actual))
                    failed = True

            print("Test 2: names are zero-padded to two digits")
            if predbat_log_name(1) != "predbat.01.log" or predbat_log_name(99) != "predbat.99.log":
                print("  ERROR: unexpected padded names: {!r} {!r}".format(predbat_log_name(1), predbat_log_name(99)))
                failed = True
            if predbat_log_name_legacy(1) != "predbat.1.log":
                print("  ERROR: unexpected legacy name: {!r}".format(predbat_log_name_legacy(1)))
                failed = True

            print("Test 3: a rotation shifts every log up one slot")
            _touch("predbat.log", "current")
            _touch(predbat_log_name(1), "one")
            _touch(predbat_log_name(2), "two")
            _rotate({"log_count": 10})
            for name, expected in ((predbat_log_name(1), "current"), (predbat_log_name(2), "one"), (predbat_log_name(3), "two")):
                if not os.path.isfile(name):
                    print("  ERROR: expected {} to exist after rotation".format(name))
                    failed = True
                elif open(name, encoding="utf-8").read() != expected:
                    print("  ERROR: {} should hold {!r}, got {!r}".format(name, expected, open(name, encoding="utf-8").read()))
                    failed = True

            print("Test 4: single-digit logs from an older Predbat migrate to two-digit")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            _touch(predbat_log_name_legacy(1), "old-one")
            _touch(predbat_log_name_legacy(2), "old-two")
            _rotate({"log_count": 10})
            if os.path.isfile(predbat_log_name_legacy(2)) or os.path.isfile(predbat_log_name_legacy(3)):
                print("  ERROR: single-digit files should have been renamed, found {}".format(sorted(os.listdir("."))))
                failed = True
            for name, expected in ((predbat_log_name(2), "old-one"), (predbat_log_name(3), "old-two")):
                if not os.path.isfile(name) or open(name, encoding="utf-8").read() != expected:
                    print("  ERROR: expected {} to hold {!r}, listing is {}".format(name, expected, sorted(os.listdir("."))))
                    failed = True

            print("Test 4b: a legacy single-digit log in the OLDEST kept slot is migrated, not stranded")
            # #5076 Copilot review: the shift previously only walked slots 1..max_logs-1, so a
            # legacy predbat.9.log sitting in the oldest kept slot (max_logs=9 for the default
            # log_count=10) was never a rename source - it does not collide with anything landing
            # on slot 9 from below, since that only writes the two-digit name. Left on disk under
            # the old name forever, invisible to predbat_log_file_prev()'s two-digit-first lookup.
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            _touch(predbat_log_name_legacy(9), "oldest-legacy")
            _rotate({"log_count": 10})  # max_logs = 9
            if os.path.isfile(predbat_log_name_legacy(9)):
                print("  ERROR: the legacy file in the oldest kept slot should not survive under its old name, found {}".format(sorted(os.listdir("."))))
                failed = True
            if os.path.isfile(predbat_log_name(10)) or os.path.isfile(predbat_log_name_legacy(10)):
                print("  ERROR: log_count=10 keeps only slots 1-9 plus the live log, found {}".format(sorted(os.listdir("."))))
                failed = True

            print("Test 5: logs beyond the configured count are removed")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            for num in range(1, 6):
                _touch(predbat_log_name(num), "log{}".format(num))
            _rotate({"log_count": 3})
            # Keeping 3 files total means the live log plus slots 01 and 02.
            if os.path.isfile(predbat_log_name(3)):
                print("  ERROR: slot 03 should have been removed with log_count 3, listing is {}".format(sorted(os.listdir("."))))
                failed = True
            remaining = sorted(name for name in os.listdir(".") if name != "predbat.log")
            if remaining != [predbat_log_name(1), predbat_log_name(2)]:
                print("  ERROR: expected only slots 01 and 02 to remain, got {}".format(remaining))
                failed = True

            print("Test 6: lowering log_count clears files stranded above the new count")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            for num in range(1, 21):
                _touch(predbat_log_name(num), "log{}".format(num))
            _rotate({"log_count": 4})
            stranded = [name for name in os.listdir(".") if name not in ("predbat.log", predbat_log_name(1), predbat_log_name(2), predbat_log_name(3))]
            if stranded:
                print("  ERROR: files above the new count should be removed, found {}".format(sorted(stranded)))
                failed = True

            print("Test 7: the viewer prefers the two-digit previous log")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "live-line")
            _touch(predbat_log_name(1), "padded-prev")
            if predbat_log_file_prev() != predbat_log_name(1):
                print("  ERROR: expected the padded name to win, got {!r}".format(predbat_log_file_prev()))
                failed = True
            data = read_predbat_log()
            if "padded-prev" not in data or "live-line" not in data:
                print("  ERROR: the viewer should show both logs, got {!r}".format(data))
                failed = True
            if data.index("padded-prev") > data.index("live-line"):
                print("  ERROR: the previous log should be prefixed, not appended, got {!r}".format(data))
                failed = True

            print("Test 8: the viewer falls back to a single-digit previous log after an upgrade")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "live-line")
            _touch(predbat_log_name_legacy(1), "legacy-prev")
            if predbat_log_file_prev() != predbat_log_name_legacy(1):
                print("  ERROR: expected the legacy name as fallback, got {!r}".format(predbat_log_file_prev()))
                failed = True
            data = read_predbat_log()
            if "legacy-prev" not in data:
                print("  ERROR: an upgrade must not lose the previous log, got {!r}".format(data))
                failed = True

            print("Test 9: no previous log at all is not an error")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "only-line")
            if predbat_log_file_prev() is not None:
                print("  ERROR: expected no previous log, got {!r}".format(predbat_log_file_prev()))
                failed = True
            if read_predbat_log().strip() != "only-line":
                print("  ERROR: expected just the live log, got {!r}".format(read_predbat_log()))
                failed = True
    finally:
        os.chdir(saved_cwd)

    if failed:
        print("\n**** log_rotation tests: FAILED ****")
    else:
        print("\n**** log_rotation tests: PASSED ****")

    # Thread-safety of rotation and shutdown, alongside the retention/naming checks above
    failed |= test_write_survives_a_closed_handle()
    failed |= test_write_retries_onto_the_new_handle()
    failed |= test_rotation_publishes_before_closing()
    failed |= test_shutdown_leaves_logfile_open_for_surviving_threads()
    failed |= test_component_threads_are_daemon()
    failed |= test_rotation_rolls_back_when_the_replacement_cannot_be_opened()
    return failed
