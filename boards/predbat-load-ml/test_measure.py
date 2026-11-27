# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the predbat-load-ml measurement and board file. Run: python3 -m unittest discover -s boards/predbat-load-ml"""

import fnmatch
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


def run(seconds, peak, after=130.0, digest="abc", epochs=900, val_mae=0.018432, ok=True):
    """One benchmark result with the given figures."""
    return {
        "ok": ok,
        "training_seconds": seconds,
        "training_cpu_seconds": seconds * 4,
        "training_tick_seconds": seconds - 10,
        "rss_before_mb": 90.0,
        "peak_rss_mb": peak,
        "rss_after_mb": after,
        "forecast_digest": digest,
        "val_mae": val_mae,
        "epochs_trained": epochs,
        "peak_method": "clear_refs",
        "queued_seconds": 0.0,
        "problems": [],
    }


BASE = [run(400, 200), run(404, 201), run(398, 199)]


class TestVerdict(unittest.TestCase):
    """verdict() needs both cuts, the same training, and a retained RSS within 10%."""

    def test_passes_with_both_cuts_and_same_training(self):
        """25% off the time and off the peak, the same digest, epochs and val_mae: pass."""
        passed, lines = measure.verdict(BASE, [run(300, 150), run(299, 149), run(301, 150)])
        self.assertTrue(passed, lines)
        self.assertIn("RESULT: pass", lines[-1])

    def test_fails_when_only_time_is_cut(self):
        """A faster training with the same peak does not meet the goal."""
        passed, lines = measure.verdict(BASE, [run(300, 200)] * 3)
        self.assertFalse(passed)
        self.assertIn("peak", lines[-1])

    def test_fails_when_only_peak_is_cut(self):
        """A smaller peak with the same time does not meet the goal."""
        passed, lines = measure.verdict(BASE, [run(400, 150)] * 3)
        self.assertFalse(passed)
        self.assertIn("training time", lines[-1])

    def test_fails_when_the_digest_differs_whatever_the_saving(self):
        """A changed forecast fails, however large the saving."""
        passed, lines = measure.verdict(BASE, [run(100, 100, digest="other")] * 3)
        self.assertFalse(passed)
        self.assertIn("forecast", lines[-1])

    def test_fails_when_epochs_differ(self):
        """Training for a different number of epochs fails, even with the same forecast."""
        passed, lines = measure.verdict(BASE, [run(300, 150, epochs=800)] * 3)
        self.assertFalse(passed)
        self.assertIn("epochs", lines[-1])

    def test_val_mae_compares_at_four_dp(self):
        """val_mae must match the base's at 4 dp: 0.01846 against 0.01843 fails, 0.018432 against 0.018434 passes."""
        base = [run(400, 200, val_mae=0.01843)] * 3
        self.assertFalse(measure.verdict(base, [run(300, 150, val_mae=0.01846)] * 3)[0])
        base = [run(400, 200, val_mae=0.018432)] * 3
        self.assertTrue(measure.verdict(base, [run(300, 150, val_mae=0.018434)] * 3)[0])

    def test_retained_rss_up_to_ten_percent_higher_passes(self):
        """The RSS kept after training may rise by up to 10%, and is reported."""
        passed, lines = measure.verdict(BASE, [run(300, 150, after=143.0)] * 3)
        self.assertTrue(passed, lines)
        self.assertTrue(any("after training" in line for line in lines))

    def test_retained_rss_more_than_ten_percent_higher_fails(self):
        """More than 10% more RSS kept after training fails."""
        passed, lines = measure.verdict(BASE, [run(300, 150, after=144.0)] * 3)
        self.assertFalse(passed)

    def test_a_run_not_ok_fails(self):
        """Any run that is not ok fails the candidate."""
        passed, lines = measure.verdict(BASE, [run(300, 150), run(300, 150, ok=False), run(300, 150)])
        self.assertFalse(passed)
        self.assertIn("ok false", lines[-1])

    def test_uses_medians(self):
        """One slow outlier does not decide: the median is judged."""
        self.assertTrue(measure.verdict(BASE, [run(300, 150), run(900, 150), run(299, 149)])[0])

    def test_a_base_disagreeing_with_itself_is_a_harness_error(self):
        """A base whose digest, epochs or val_mae differ between its runs means the benchmark is not deterministic."""
        for odd in (run(400, 200, digest="x"), run(400, 200, epochs=1), run(400, 200, val_mae=0.5)):
            with self.assertRaises(measure.HarnessError):
                measure.verdict([run(400, 200), odd, run(400, 200)], [run(300, 150)] * 3)


class TestLoadResult(unittest.TestCase):
    """load_result checks every field the verdict reads, and refuses profiled runs."""

    def write(self, tmp, data):
        """Write data as result.json in tmp and return its path."""
        path = Path(tmp) / "result.json"
        path.write_text(json.dumps(data))
        return path

    def test_reads_a_complete_result(self):
        """A complete result is returned as read."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(measure.load_result(self.write(tmp, run(400, 200)))["peak_rss_mb"], 200)

    def test_rejects_a_missing_field(self):
        """Each field the verdict reads must be there."""
        for field in ("ok", "training_seconds", "peak_rss_mb", "rss_after_mb", "forecast_digest", "epochs_trained", "val_mae"):
            data = run(400, 200)
            del data[field]
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(measure.HarnessError):
                    measure.load_result(self.write(tmp, data))

    def test_rejects_a_boolean_where_a_number_belongs(self):
        """True is an int to Python, so it is refused explicitly."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(measure.HarnessError):
                measure.load_result(self.write(tmp, dict(run(400, 200), epochs_trained=True)))

    def test_rejects_figures_that_are_not_finite_or_negative(self):
        """NaN, infinity and negative figures are refused."""
        for value in (float("nan"), float("inf"), -1.0):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "result.json"
                path.write_text(json.dumps(dict(run(400, 200), training_seconds=value)))
                with self.assertRaises(measure.HarnessError):
                    measure.load_result(path)

    def test_rejects_a_tracemalloc_run(self):
        """A --tracemalloc run carries tracemalloc's overhead."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(measure.HarnessError):
                measure.load_result(self.write(tmp, dict(run(400, 200), tracemalloc=True)))

    def test_rejects_a_cprofile_run(self):
        """A --profile run carries cProfile's overhead in its time."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(measure.HarnessError):
                measure.load_result(self.write(tmp, dict(run(400, 200), profiled=True)))


class TestRunBenchmark(unittest.TestCase):
    """_run_benchmark turns every way the benchmark can fail to run into a harness error."""

    def test_a_benchmark_that_fails_is_a_harness_error(self):
        """A non-zero exit from the benchmark raises HarnessError naming its exit code."""
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
            result = subprocess.run([sys.executable, str(Path(measure.__file__).resolve())], cwd=repo, env=dict(os.environ, BOARD_CANDIDATE="some-branch"), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("HARNESS ERROR", result.stdout)

    def test_missing_candidate_is_a_harness_error(self):
        """No BOARD_CANDIDATE exits 2."""
        env = {key: value for key, value in os.environ.items() if key != "BOARD_CANDIDATE"}
        result = subprocess.run([sys.executable, str(Path(measure.__file__).resolve())], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("BOARD_CANDIDATE is not set", result.stdout)


WORKTREE_GIT = "Bash(git -C */.worktrees/board-loadml-* "
MAIN = "/Users/someone/predbat/batpred"
WT = "git -C " + MAIN + "/.worktrees/board-loadml-1a2b3c4d"
GIT_READS = ("status", "diff", "log", "show", "rev-parse", "rev-list", "merge-base", "branch", "worktree")


class TestBoardFile(unittest.TestCase):
    """The board file lets workers do the work, denies the dangerous commands, and carries the spec's goal and limits."""

    def setUp(self):
        """Load board.toml."""
        self.board = tomllib.loads((HERE / "board.toml").read_text())

    def permitted(self, command):
        """Whether a worker may run command: an allow entry matches from the start (`*` spans anything) and no deny does."""

        def matches(entry):
            """Whether one Bash(...) entry matches command."""
            return entry.startswith("Bash(") and fnmatch.fnmatchcase(command, entry[len("Bash(") : -1])

        return any(matches(e) for e in self.board["permissions"]["allow"]) and not any(matches(e) for e in self.board["permissions"]["deny"])

    def test_the_benchmark_and_its_profile_mode_are_allowed(self):
        """Workers run coverage/load_ml_benchmark, plain and with --profile or --tracemalloc, from the main checkout."""
        for command in ("coverage/load_ml_benchmark " + MAIN + "/.worktrees/board-loadml-1a2b3c4d /tmp/predbat-board/load-ml/1a2b3c4d/run1", "coverage/load_ml_benchmark --profile " + MAIN + " /tmp/predbat-board/load-ml/1a2b3c4d/main"):
            self.assertTrue(self.permitted(command), command)
        self.assertIn("--profile", self.board["rules"])
        self.assertIn("--tracemalloc", self.board["rules"])

    def test_git_allow_entries_are_reads_or_scoped_writes(self):
        """Every git allow entry is a read, plain or -C, or a write naming a board-loadml worktree."""
        for entry in self.board["permissions"]["allow"]:
            if entry.startswith("Bash(git "):
                body = entry[len("Bash(git ") : -1]
                read = body.removeprefix("-C * ").split(" ")[0].rstrip("*") in GIT_READS
                self.assertTrue(read or entry.startswith(WORKTREE_GIT), entry)

    def test_the_commands_workers_need_are_permitted(self):
        """The everyday commands, including a commit whose message mentions denied words."""
        for command in (
            WT + " commit -F /tmp/predbat-board/load-ml/1a2b3c4d/message.txt",
            WT + " commit -m 'board(predbat-load-ml 1a2b3c4d): build the windowed features once and push the copy out'",
            WT + " cherry-pick -x 610f6151",
            "git worktree add -b board/predbat-load-ml/1a2b3c4d " + MAIN + "/.worktrees/board-loadml-1a2b3c4d main",
            "git log --oneline main..board/predbat-load-ml/7e4d2dbd",
            "python3 -c 'import pstats'",
            "mkdir -p /tmp/predbat-board/load-ml/1a2b3c4d",
        ):
            self.assertTrue(self.permitted(command), command)

    def test_the_mistakes_the_list_is_there_to_stop_are_refused(self):
        """Moving main, pushing, deleting another intent's branch or worktree, writes that escape, and pip."""
        for command in (
            "git commit -m x",
            "git -C " + MAIN + " commit -m x",
            WT + "/../.. commit -m x",
            "git push",
            WT + " push",
            WT + " reset --hard HEAD~1",
            "git branch -D board/predbat-load-ml/7e4d2dbd",
            "git worktree remove --force " + MAIN + "/.worktrees/board-loadml-7e4d2dbd",
            "git diff --output=apps/predbat/predbat.py",
            "python3 -m pip install requests",
            "rm -rf /tmp/predbat-board",
            "gh pr create",
            "coverage/memory_benchmark " + MAIN + " /tmp/x",
        ):
            self.assertFalse(self.permitted(command), command)

    def test_no_allow_entry_is_a_catch_all(self):
        """No rm or env allow entry: no spelling-based deny makes them safe."""
        for entry in self.board["permissions"]["allow"]:
            self.assertFalse(entry.startswith("Bash(rm") or entry.startswith("Bash(env"), entry)

    def test_the_main_checkout_tools_and_board_files_are_not_editable(self):
        """Workers edit only their own worktrees and scratch: never apps/, coverage/ or boards/ in the main checkout."""
        deny = self.board["permissions"]["deny"]
        for area in ("apps", "coverage", "boards"):
            self.assertIn("Edit(~/predbat/batpred/{}/**)".format(area), deny)
        self.assertIn("mcp__gitnexus__rename", deny)

    def test_the_rules_forbid_training_less(self):
        """Every way of winning by doing less training is named in the rules."""
        rules = self.board["rules"]
        for word in ("epochs", "patience", "curriculum", "learning rate", "network", "history", "holdout", "diagnostics"):
            self.assertIn(word, rules)

    def test_the_rules_carry_the_stop_rule(self):
        """The lead stops and reports, with the figures, when the time cut is out of reach under bit-identical maths."""
        self.assertIn("Stop rule", self.board["rules"])
        self.assertIn("bit-identical", self.board["rules"])

    def test_the_rules_tell_workers_how_to_spell_shell_commands(self):
        """The two ways memory-board workers got lost: the cd-and-git chain, and no closing BOARD-WORK line."""
        self.assertIn("Never put `cd` and `git` in the same call", self.board["rules"])
        self.assertIn("end with a BOARD-WORK line", self.board["rules"])

    def test_the_rules_run_the_test_suites_under_the_benchmark_lock(self):
        """./run_all --quick and ./run_pre_commit load the CPU for minutes, so the rules run them under the benchmark lock
        and a worker's tests never overlap another worker's measured run; the spelling they give is permitted."""
        rules = self.board["rules"]
        self.assertIn("bench_lock.py /tmp/predbat-load-ml-benchmark.lock -- ./run_all --quick", rules)
        self.assertIn("bench_lock.py /tmp/predbat-load-ml-benchmark.lock -- ./run_pre_commit", rules)
        self.assertTrue(self.permitted("python3 " + MAIN + "/coverage/memory_benchmark_tools/bench_lock.py /tmp/predbat-load-ml-benchmark.lock -- ./run_all --quick"))

    def test_goal_and_done_when_match_measure(self):
        """The goal names both 25% cuts and dp2, and done-when the same limits measure.py applies."""
        self.assertEqual((measure.TIME_CUT, measure.PEAK_CUT, measure.AFTER_RISE, measure.ROUNDS), (0.25, 0.25, 0.10, 3))
        self.assertIn("25%", self.board["goal"])
        self.assertIn("2 dp", self.board["goal"])
        self.assertIn("no more than 10% above main's", self.board["done_when"])
        self.assertIn("4 dp", self.board["done_when"])

    def test_limits_match_the_spec(self):
        """Two open intents, 20 in all, 120-minute claims, a 180-minute measurement and 150-minute workers."""
        self.assertEqual((self.board["max_open_intents"], self.board["max_intents"], self.board["claim_minutes"]), (2, 20, 120))
        self.assertEqual(self.board["measure"]["timeout_minutes"], 180)
        self.assertEqual(self.board["permissions"]["worker_timeout_minutes"], 150)
        self.assertEqual(self.board["measure"]["command"], "python3 boards/predbat-load-ml/measure.py")


if __name__ == "__main__":
    unittest.main()
