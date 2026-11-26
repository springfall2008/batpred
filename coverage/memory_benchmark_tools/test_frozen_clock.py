# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the frozen-clock sleep shim the benchmark container loads as sitecustomize."""

import subprocess
import sys
import unittest
from pathlib import Path

SHIM_DIR = Path(__file__).resolve().parent / "frozen_clock"

PROBE = """
import time
assert time.sleep.__module__ == "sitecustomize", time.sleep.__module__
start = time.monotonic()
time.sleep(0.2)
elapsed = time.monotonic() - start
assert 0.15 < elapsed < 2.0, elapsed
try:
    time.sleep(-1)
except ValueError:
    print("ok")
"""


class TestFrozenClockShim(unittest.TestCase):
    """The shim replaces time.sleep with a wait that works under libfaketime's frozen clock."""

    def test_shim_replaces_sleep_with_a_working_wait(self):
        """Loaded as sitecustomize, time.sleep still sleeps for the time asked and rejects a negative length."""
        proc = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env={"PYTHONPATH": str(SHIM_DIR)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
