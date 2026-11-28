# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for bench_lock, the machine-wide lock that keeps LoadML benchmark runs from overlapping."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_lock  # noqa: E402

HOLDER = "import fcntl, sys, time\nf = open(sys.argv[1], 'a')\nfcntl.flock(f, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(float(sys.argv[2]))\n"


def start_holder(lock, seconds):
    """Start a child process that takes the lock and holds it for seconds; return it once it holds the lock."""
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock), str(seconds)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


class TestHeld(unittest.TestCase):
    """held() takes the lock exclusively, says when it has to wait, and reports how long it waited."""

    def test_held_yields_zero_when_free(self):
        """A free lock is taken at once, with no waiting message."""
        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            with bench_lock.held(str(Path(tmp) / "lock"), notify=messages.append) as waited:
                self.assertLess(waited, 0.5)
        self.assertEqual(messages, [])

    def test_second_holder_waits_and_is_told(self):
        """A busy lock makes the caller wait for it, and the caller is told once that it is waiting."""
        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            holder = start_holder(lock, 1.0)
            try:
                with bench_lock.held(str(lock), notify=messages.append) as waited:
                    self.assertGreaterEqual(waited, 0.8)
            finally:
                holder.wait()
        self.assertEqual(len(messages), 1)
        self.assertIn("Waiting for the benchmark lock", messages[0])

    def test_a_killed_holder_does_not_block(self):
        """A holder that dies (a timed-out worker killed by the runner) releases the lock with it."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            holder = start_holder(lock, 60)
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait()
            began = time.monotonic()
            with bench_lock.held(str(lock), notify=lambda message: None):
                pass
            self.assertLess(time.monotonic() - began, 1.0)


class TestMain(unittest.TestCase):
    """main() runs a command under the lock and passes its exit code through."""

    def test_main_passes_queued_seconds_and_exit_code(self):
        """The command sees BENCH_QUEUED_SECONDS, and its exit code is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = str(Path(tmp) / "lock")
            check = "import os, sys; sys.exit(0 if os.environ['BENCH_QUEUED_SECONDS'] == '0.0' else 5)"
            self.assertEqual(bench_lock.main([lock, "--", sys.executable, "-c", check]), 0)
            self.assertEqual(bench_lock.main([lock, "--", sys.executable, "-c", "import sys; sys.exit(3)"]), 3)

    def test_main_usage_error_returns_2(self):
        """No command, or no -- separator, is a usage error."""
        self.assertEqual(bench_lock.main([]), 2)
        self.assertEqual(bench_lock.main(["/tmp/x.lock", "true"]), 2)
        self.assertEqual(bench_lock.main(["/tmp/x.lock", "--"]), 2)


if __name__ == "__main__":
    unittest.main()
