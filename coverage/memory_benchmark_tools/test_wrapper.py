# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for coverage/memory_benchmark's argument and fixture checks, which run before any container is started."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "memory_benchmark"
REPO = WRAPPER.parent.parent


def run_wrapper(*args, data=None):
    """Run the wrapper with the fixture directory overridden; return (exit code, output)."""
    env = dict(os.environ)
    if data is not None:
        env["MEMORY_BENCHMARK_DATA"] = str(data)
    proc = subprocess.run(["bash", str(WRAPPER), *map(str, args)], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr


class TestWrapperChecks(unittest.TestCase):
    """The wrapper refuses to start a container without a tree and a fixture."""

    def test_wrapper_without_a_fixture_exits_2_and_says_how_to_build_it(self):
        """No manifest in the fixture directory: exit 2, name the export script, write no result.json."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code, output = run_wrapper(REPO, out, data=Path(tmp) / "no-fixture")
            self.assertEqual(code, 2, output)
            self.assertIn("export_fixture.py", output)
            self.assertFalse((out / "result.json").exists())

    def test_wrapper_rejects_a_missing_tree(self):
        """A tree that is not a batpred checkout: exit 2."""
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_wrapper(Path(tmp) / "nowhere", Path(tmp) / "out", data=Path(tmp))
            self.assertEqual(code, 2, output)
            self.assertIn("not a batpred checkout", output)

    def test_wrapper_needs_two_arguments(self):
        """Usage error without both arguments: exit 2 with the usage line."""
        code, output = run_wrapper(REPO)
        self.assertEqual(code, 2)
        self.assertIn("usage: memory_benchmark [--tracemalloc] <tree> <out dir>", output)

    def test_wrapper_takes_tracemalloc_before_the_tree(self):
        """--tracemalloc then a tree and an out dir is a valid call: it gets as far as the fixture check."""
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_wrapper("--tracemalloc", REPO, Path(tmp) / "out", data=Path(tmp) / "no-fixture")
            self.assertEqual(code, 2, output)
            self.assertIn("export_fixture.py", output)

    def test_wrapper_with_tracemalloc_still_needs_two_arguments(self):
        """--tracemalloc does not stand in for the tree or the out dir."""
        code, output = run_wrapper("--tracemalloc", REPO)
        self.assertEqual(code, 2)
        self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
