# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the predbat-memory measurement and board file. Run: python3 -m unittest discover -s boards/predbat-memory"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import measure  # noqa: E402


def run(steady, peak, digest="abc", ok=True):
    """One benchmark result with the given figures."""
    return {"ok": ok, "steady_rss_mb": steady, "peak_rss_mb": peak, "plan_digest": digest}


BASE = [run(100, 150), run(102, 151), run(101, 149)]


class TestVerdict(unittest.TestCase):
    """verdict compares the base's and the candidate's runs against the goal."""

    def test_passes_at_a_quarter_cut_with_same_plans(self):
        """A 25% steady cut with the same digest passes."""
        passed, lines = measure.verdict(BASE, [run(75, 140), run(76, 141), run(74, 139)])
        self.assertTrue(passed, lines)
        self.assertEqual(lines[-1], "RESULT: pass")

    def test_fails_below_a_quarter_cut(self):
        """A 20% cut is not enough."""
        passed, lines = measure.verdict(BASE, [run(80, 140)] * 3)
        self.assertFalse(passed)
        self.assertIn("steady-state cut", lines[-1])

    def test_fails_when_plans_change_whatever_the_saving(self):
        """A different digest fails however much memory it saves."""
        passed, lines = measure.verdict(BASE, [run(50, 100, digest="other")] * 3)
        self.assertFalse(passed)
        self.assertIn("plans changed", lines[-1])

    def test_a_peak_up_to_ten_percent_higher_passes_and_is_reported(self):
        """The goal is steady memory: a median peak within 10% of the base's passes (165 is exactly 10% over 150)."""
        for peak in (160, 165):
            passed, lines = measure.verdict(BASE, [run(70, peak)] * 3)
            self.assertTrue(passed, (peak, lines))
        self.assertIn("median peak: base 150.0 MB, candidate 165.0 MB, +10.0% (limit +10%)", lines)

    def test_a_peak_more_than_ten_percent_higher_fails(self):
        """A steady saving bought with a much higher peak fails: 166 MB is more than 10% over 150 MB."""
        passed, lines = measure.verdict(BASE, [run(70, 166)] * 3)
        self.assertFalse(passed)
        self.assertIn("median peak RSS is 10.7% above the base's", lines[-1])

    def test_fails_when_a_run_is_not_ok(self):
        """A run that reported ok false fails the candidate."""
        passed, lines = measure.verdict(BASE, [run(70, 140), run(70, 140, ok=False), run(70, 140)])
        self.assertFalse(passed)
        self.assertIn("ok false", lines[-1])

    def test_uses_medians_so_one_outlier_does_not_decide(self):
        """One bad run among three does not decide the verdict."""
        passed, lines = measure.verdict(BASE, [run(75, 140), run(200, 300), run(74, 139)])
        self.assertTrue(passed, lines)

    def test_a_nondeterministic_base_is_a_harness_error(self):
        """A base whose digest differs between its own runs raises, so main exits 2 and the candidate is not judged."""
        with self.assertRaises(measure.HarnessError) as caught:
            measure.verdict([run(100, 150, "a"), run(100, 150, "b"), run(100, 150, "a")], [run(70, 140, "a")] * 3)
        self.assertIn("not deterministic", str(caught.exception))


class TestLoadResult(unittest.TestCase):
    """load_result accepts only a complete result with real figures."""

    def write_text(self, text):
        """Write text to a temporary result file and return its path."""
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(text)
        handle.close()
        return handle.name

    def write(self, data):
        """Write data as JSON the way json.dump does, NaN and Infinity included."""
        return self.write_text(json.dumps(data))

    def test_reads_a_complete_result(self):
        """A result with every field is returned as read."""
        self.assertEqual(measure.load_result(self.write(run(1.5, 2))), run(1.5, 2))

    def test_rejects_a_missing_field(self):
        """A result without plan_digest is a harness error."""
        data = run(1, 2)
        del data["plan_digest"]
        with self.assertRaises(measure.HarnessError):
            measure.load_result(self.write(data))

    def test_rejects_a_boolean_where_a_number_belongs(self):
        """True is not a figure."""
        with self.assertRaises(measure.HarnessError):
            measure.load_result(self.write(run(True, 2)))

    def test_rejects_figures_that_are_not_finite(self):
        """NaN or Infinity (json.dump writes both by default) would slip past every comparison in verdict."""
        for steady, peak in ((float("nan"), 2), (1, float("nan")), (float("inf"), 2), (1, float("-inf"))):
            with self.assertRaises(measure.HarnessError, msg=(steady, peak)):
                measure.load_result(self.write(run(steady, peak)))

    def test_rejects_a_negative_figure(self):
        """A negative RSS cannot be a measurement, and would make the cut meaningless."""
        with self.assertRaises(measure.HarnessError):
            measure.load_result(self.write(run(-1, 2)))

    def test_rejects_a_profiled_run(self):
        """A --tracemalloc run's RSS includes tracemalloc's own overhead, so it is no evidence for a verdict."""
        with self.assertRaises(measure.HarnessError) as caught:
            measure.load_result(self.write(dict(run(1, 2), tracemalloc=True)))
        self.assertIn("tracemalloc", str(caught.exception))


class TestRunBenchmark(unittest.TestCase):
    """_run_benchmark turns every way the benchmark can fail to run into a harness error."""

    def test_a_benchmark_that_cannot_be_started_is_a_harness_error(self):
        """A benchmark path that is not a program raises HarnessError, not OSError, so main exits 2."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(measure.HarnessError):
                measure._run_benchmark(Path(tmp), Path(tmp), Path(tmp) / "out")

    def test_a_benchmark_that_fails_is_a_harness_error(self):
        """A non-zero exit from the benchmark raises HarnessError naming its log."""
        with tempfile.TemporaryDirectory() as tmp:
            failing = Path(tmp) / "bench"
            failing.write_text("#!/bin/sh\nexit 3\n")
            failing.chmod(0o755)
            with self.assertRaises(measure.HarnessError) as caught:
                measure._run_benchmark(failing, Path(tmp), Path(tmp) / "out")
            self.assertIn("exited 3", str(caught.exception))


class TestMain(unittest.TestCase):
    """main's exit codes for a measurement that cannot be made."""

    def test_missing_benchmark_is_a_harness_error(self):
        """A checkout without the benchmark exits 2."""
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
        """No BOARD_CANDIDATE exits 2."""
        env = {key: value for key, value in os.environ.items() if key != "BOARD_CANDIDATE"}
        result = subprocess.run([sys.executable, str(Path(measure.__file__).resolve())], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("BOARD_CANDIDATE is not set", result.stdout)


# What a worker needs for the intents the lead writes: Python to profile (importtime, gc referrers, deeper tracemalloc),
# merge and rebase to combine earlier board commits, and the everyday read-only tools
NEEDED = ("Bash(python3 *)", "Bash(*/coverage/venv/bin/python3 *)", "Bash(git merge *)", "Bash(git rebase *)", "Bash(find *)", "Bash(sort *)", "Bash(diff *)", "Bash(docker ps*)")

# The mistakes the deny list is there to stop: anything that leaves the machine, rewrites main or another intent's
# branch, throws away work, or changes the shared venv
DANGEROUS = ("Bash(git push*)", "Bash(gh *)", "Bash(git reset --hard*)", "Bash(git branch -D*)", "Bash(git branch -f*)", "Bash(git update-ref*)", "Bash(rm -rf*)", "Bash(pip *)", "Edit(~/predbat/batpred/apps/**)")


class TestBoardFile(unittest.TestCase):
    """The board file lets workers do the work the intents ask for, and still denies the dangerous commands."""

    def setUp(self):
        """Load board.toml."""
        self.board = tomllib.loads((HERE / "board.toml").read_text())

    def test_workers_have_the_tools_the_intents_need(self):
        """Python, merge, rebase and the read-only tools are allowed; without them a worker can only give up."""
        for entry in NEEDED:
            self.assertIn(entry, self.board["permissions"]["allow"])

    def test_the_dangerous_commands_stay_denied(self):
        """Pushing, rewriting branches, throwing work away and pip are denied, and none of them is also allowed."""
        for entry in DANGEROUS:
            self.assertIn(entry, self.board["permissions"]["deny"])
            self.assertNotIn(entry, self.board["permissions"]["allow"])

    def test_merge_and_rebase_are_limited_to_the_workers_own_branch(self):
        """The rules say where merge and rebase may be used, since the permissions cannot."""
        self.assertIn("only on your own intent's branch", self.board["rules"])

    def test_the_benchmark_and_its_profiling_mode_are_allowed(self):
        """Workers profile with coverage/memory_benchmark --tracemalloc, which the benchmark's own entry covers."""
        self.assertIn("Bash(coverage/memory_benchmark *)", self.board["permissions"]["allow"])
        self.assertIn("--tracemalloc", self.board["rules"])

    def test_the_rules_tell_workers_how_to_spell_shell_commands(self):
        """Workers are told the cd-and-git chain is refused and to end with a BOARD-WORK line, the two ways they got lost."""
        self.assertIn("Never put `cd` and `git` in the same call", self.board["rules"])
        self.assertIn("end with a BOARD-WORK line", self.board["rules"])

    def test_the_rules_point_workers_at_the_loaded_libraries(self):
        """Cutting imported libraries is in scope, and modules.txt is where a worker sees what is loaded."""
        self.assertIn("modules.txt", self.board["rules"])
        self.assertIn("libraries", self.board["goal"])

    def test_done_when_states_the_peak_limit_measure_applies(self):
        """The done-when names the same peak limit as measure.PEAK_RISE, so the lead and the measurement agree."""
        self.assertEqual(measure.PEAK_RISE, 0.10)
        self.assertIn("no more than 10% above main's", self.board["done_when"])


class TestDocstrings(unittest.TestCase):
    """Every function and class in this directory has a docstring (CLAUDE.md's 100% interrogate rule)."""

    def test_every_function_and_class_has_a_docstring(self):
        """measure.py and this file are fully documented."""
        for path in (HERE / "measure.py", HERE / "test_measure.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.assertTrue(ast.get_docstring(node), "{}: {} has no docstring".format(path.name, node.name))


if __name__ == "__main__":
    unittest.main()
