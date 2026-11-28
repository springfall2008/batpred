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


def run_wrapper(*args, data=None, bin_dir=None):
    """Run the wrapper with the fixture directory overridden, and bin_dir first on PATH; return (exit code, output)."""
    env = dict(os.environ)
    if data is not None:
        env["MEMORY_BENCHMARK_DATA"] = str(data)
    if bin_dir is not None:
        env["PATH"] = "{}{}{}".format(bin_dir, os.pathsep, env.get("PATH", ""))
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

    def test_wrapper_clears_every_output_of_an_earlier_run(self):
        """A run that dies before the driver writes leaves no earlier result.json, modules.txt or tracemalloc.txt behind."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "fixture"
            data.mkdir()
            (data / "manifest.json").write_text('{"t0": "2026-11-26T10:56:00+00:00"}')
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text("#!/bin/sh\nexit 1\n")  # every docker call fails, so the run dies before any container starts
            docker.chmod(0o755)
            out = Path(tmp) / "out"
            out.mkdir()
            stale = ("result.json", "modules.txt", "tracemalloc.txt")
            for name in stale:
                (out / name).write_text("from an earlier run")
            code, output = run_wrapper(REPO, out, data=data, bin_dir=bin_dir)
            self.assertNotEqual(code, 0, output)
            for name in stale:
                self.assertFalse((out / name).exists(), name)

    def test_wrapper_mounts_a_fresh_copy_of_the_tree_not_the_tree_itself(self):
        """The container reads a copy made for this run, so Docker Desktop never serves an edited file at its old length."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "fixture"
            data.mkdir()
            (data / "manifest.json").write_text('{"t0": "2026-11-26T10:56:00+00:00"}')
            record = Path(tmp) / "record.txt"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            # image inspect succeeds (no build); run records the source mounted at /src and what it holds, then fails
            docker.write_text(
                "#!/bin/bash\n"
                '[ "$1" = image ] && exit 0\n'
                'while [ $# -gt 0 ]; do if [ "$1" = -v ] && [ "${2%:/src:ro}" != "$2" ]; then src="${2%:/src:ro}"; fi; shift; done\n'
                'echo "$src" > "{record}"; ls "$src" >> "{record}"\n'
                "exit 1\n".replace("{record}", str(record))
            )
            docker.chmod(0o755)
            code, output = run_wrapper(REPO, Path(tmp) / "out", data=data, bin_dir=bin_dir)
            self.assertNotEqual(code, 0, output)
            lines = record.read_text().splitlines()
        self.assertNotEqual(Path(lines[0]).resolve(), (REPO / "apps" / "predbat").resolve())
        self.assertIn("predbat.py", lines[1:])

    def test_wrapper_with_tracemalloc_still_needs_two_arguments(self):
        """--tracemalloc does not stand in for the tree or the out dir."""
        code, output = run_wrapper("--tracemalloc", REPO)
        self.assertEqual(code, 2)
        self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
