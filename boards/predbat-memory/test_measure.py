# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the predbat-memory measurement. Run: python3 -m unittest discover -s boards/predbat-memory"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import measure  # noqa: E402


def run(steady, peak, digest="abc", ok=True):
    return {"ok": ok, "steady_rss_mb": steady, "peak_rss_mb": peak, "plan_digest": digest}


BASE = [run(100, 150), run(102, 151), run(101, 149)]


class TestVerdict(unittest.TestCase):
    def test_passes_at_a_quarter_cut_with_same_plans_and_lower_peak(self):
        passed, lines = measure.verdict(BASE, [run(75, 140), run(76, 141), run(74, 139)])
        self.assertTrue(passed, lines)
        self.assertEqual(lines[-1], "RESULT: pass")

    def test_fails_below_a_quarter_cut(self):
        passed, lines = measure.verdict(BASE, [run(80, 140)] * 3)
        self.assertFalse(passed)
        self.assertIn("steady-state cut", lines[-1])

    def test_fails_when_plans_change_whatever_the_saving(self):
        passed, lines = measure.verdict(BASE, [run(50, 100, digest="other")] * 3)
        self.assertFalse(passed)
        self.assertIn("plans changed", lines[-1])

    def test_fails_when_peak_rises(self):
        passed, lines = measure.verdict(BASE, [run(70, 160)] * 3)
        self.assertFalse(passed)
        self.assertIn("peak", lines[-1])

    def test_fails_when_a_run_is_not_ok(self):
        passed, lines = measure.verdict(BASE, [run(70, 140), run(70, 140, ok=False), run(70, 140)])
        self.assertFalse(passed)
        self.assertIn("ok false", lines[-1])

    def test_uses_medians_so_one_outlier_does_not_decide(self):
        passed, lines = measure.verdict(BASE, [run(75, 140), run(200, 300), run(74, 139)])
        self.assertTrue(passed, lines)

    def test_a_nondeterministic_base_is_a_harness_error(self):
        passed, lines = measure.verdict([run(100, 150, "a"), run(100, 150, "b"), run(100, 150, "a")], [run(70, 140, "a")] * 3)
        self.assertFalse(passed)
        self.assertTrue(lines[-1].startswith("HARNESS ERROR"))


class TestLoadResult(unittest.TestCase):
    def write(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(json.dumps(data))
        handle.close()
        return handle.name

    def test_reads_a_complete_result(self):
        self.assertEqual(measure.load_result(self.write(run(1.5, 2))), run(1.5, 2))

    def test_rejects_a_missing_field(self):
        data = run(1, 2)
        del data["plan_digest"]
        with self.assertRaises(measure.HarnessError):
            measure.load_result(self.write(data))

    def test_rejects_a_boolean_where_a_number_belongs(self):
        with self.assertRaises(measure.HarnessError):
            measure.load_result(self.write(run(True, 2)))


class TestMain(unittest.TestCase):
    def test_missing_benchmark_is_a_harness_error(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", repo], check=True)
            result = subprocess.run(
                [sys.executable, str(Path(measure.__file__).resolve())],
                cwd=repo,
                env=dict(os.environ, BOARD_CANDIDATE="some-branch"),
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("HARNESS ERROR", result.stdout)
        self.assertIn("has not been built yet", result.stdout)

    def test_missing_candidate_is_a_harness_error(self):
        env = {key: value for key, value in os.environ.items() if key != "BOARD_CANDIDATE"}
        result = subprocess.run([sys.executable, str(Path(measure.__file__).resolve())], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("BOARD_CANDIDATE is not set", result.stdout)


if __name__ == "__main__":
    unittest.main()
