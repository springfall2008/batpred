# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for coverage/load_ml_benchmark's checks and its docker call, run against a fake docker so no container starts."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "load_ml_benchmark"
MEMORY_WRAPPER = WRAPPER.parent / "memory_benchmark"
REPO = WRAPPER.parent.parent


def run_wrapper(*args, data=None, bin_dir=None, lock=None):
    """Run the wrapper with the fixture and lock overridden and bin_dir first on PATH; return (exit code, output)."""
    env = dict(os.environ)
    if data is not None:
        env["LOAD_ML_BENCHMARK_DATA"] = str(data)
    if lock is not None:
        env["LOAD_ML_BENCHMARK_LOCK"] = str(lock)
    if bin_dir is not None:
        env["PATH"] = "{}{}{}".format(bin_dir, os.pathsep, env.get("PATH", ""))
    proc = subprocess.run(["bash", str(WRAPPER), *map(str, args)], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr


def make_fixture(tmp):
    """Write a fixture directory holding only a manifest, which is all the wrapper reads before docker runs."""
    data = Path(tmp) / "fixture"
    data.mkdir()
    (data / "manifest.json").write_text('{"t0": "2026-11-27T19:00:00+00:00"}')
    return data


def make_docker(tmp, script):
    """Write a fake docker into a bin directory and return the directory."""
    bin_dir = Path(tmp) / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(script)
    docker.chmod(0o755)
    return bin_dir


class TestLoadMlWrapperChecks(unittest.TestCase):
    """The wrapper refuses to start a container without a tree and a LoadML fixture."""

    def test_without_a_fixture_exits_2_and_names_the_load_ml_export(self):
        """No manifest: exit 2, give the --load-ml export command, write no result.json."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code, output = run_wrapper(REPO, out, data=Path(tmp) / "no-fixture")
            self.assertEqual(code, 2, output)
            self.assertIn("export_fixture.py", output)
            self.assertIn("--load-ml", output)
            self.assertFalse((out / "result.json").exists())

    def test_rejects_a_missing_tree(self):
        """A tree that is not a batpred checkout: exit 2."""
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_wrapper(Path(tmp) / "nowhere", Path(tmp) / "out", data=Path(tmp))
            self.assertEqual(code, 2, output)
            self.assertIn("not a batpred checkout", output)

    def test_needs_two_arguments(self):
        """Usage error without both arguments: exit 2 with the usage line."""
        code, output = run_wrapper(REPO)
        self.assertEqual(code, 2)
        self.assertIn("usage: load_ml_benchmark [--tracemalloc|--profile] <tree> <out dir>", output)

    def test_takes_profile_or_tracemalloc_before_the_tree(self):
        """Either flag then a tree and an out dir gets as far as the fixture check; a flag alone is not enough."""
        with tempfile.TemporaryDirectory() as tmp:
            for flag in ("--profile", "--tracemalloc"):
                code, output = run_wrapper(flag, REPO, Path(tmp) / "out", data=Path(tmp) / "no-fixture")
                self.assertEqual(code, 2, output)
                self.assertIn("--load-ml", output)
                code, output = run_wrapper(flag, REPO)
                self.assertIn("usage:", output)

    def test_clears_every_output_of_an_earlier_run(self):
        """A run that dies before the driver writes leaves none of an earlier run's outputs behind, its logs included."""
        with tempfile.TemporaryDirectory() as tmp:
            data = make_fixture(tmp)
            bin_dir = make_docker(tmp, "#!/bin/sh\nexit 1\n")
            out = Path(tmp) / "out"
            out.mkdir()
            stale = ("result.json", "modules.txt", "tracemalloc.txt", "profile.txt", "forecast.json", "predbat.log", "kernel_build.log")
            for name in stale:
                (out / name).write_text("from an earlier run")
            code, output = run_wrapper(REPO, out, data=data, bin_dir=bin_dir, lock=Path(tmp) / "lock")
            self.assertNotEqual(code, 0, output)
            for name in stale:
                self.assertFalse((out / name).exists(), name)

    def test_runs_docker_under_the_lock_with_blas_pinned(self):
        """docker run gets the training driver and BLAS pinned to 4 threads, and runs with the queue time in its environment."""
        with tempfile.TemporaryDirectory() as tmp:
            data = make_fixture(tmp)
            record = Path(tmp) / "record.txt"
            bin_dir = make_docker(tmp, '#!/bin/bash\n[ "$1" = image ] && exit 0\necho "queued=${BENCH_QUEUED_SECONDS:-unset}" > "{r}"\nprintf "%s\\n" "$@" >> "{r}"\nexit 1\n'.replace("{r}", str(record)))
            code, output = run_wrapper("--profile", REPO, Path(tmp) / "out", data=data, bin_dir=bin_dir, lock=Path(tmp) / "lock")
            self.assertNotEqual(code, 0, output)
            lines = record.read_text().splitlines()
        self.assertEqual(lines[0], "queued=0.0")
        for expected in ("BENCH_DRIVER=/bench/load_ml_driver.py", "BENCH_QUEUED_SECONDS", "OPENBLAS_NUM_THREADS=4", "OMP_NUM_THREADS=4", "MKL_NUM_THREADS=4", "BENCH_DRIVER_ARGS=--profile", "none"):
            self.assertIn(expected, lines, expected)

    def test_the_whole_run_is_inside_the_lock(self):
        """The image check (and any build) and the container all run with the lock held, so nothing heavy overlaps a run."""
        with tempfile.TemporaryDirectory() as tmp:
            data = make_fixture(tmp)
            record = Path(tmp) / "record.txt"
            bin_dir = make_docker(tmp, '#!/bin/bash\necho "$1 queued=${BENCH_QUEUED_SECONDS:-unset}" >> "{r}"\n[ "$1" = image ] && exit 0\n[ "$1" = ps ] && exit 0\nexit 1\n'.replace("{r}", str(record)))
            run_wrapper(REPO, Path(tmp) / "out", data=data, bin_dir=bin_dir, lock=Path(tmp) / "lock")
            calls = record.read_text().splitlines()
        self.assertIn("image queued=0.0", calls)
        self.assertIn("run queued=0.0", calls)

    def test_a_leftover_container_is_killed_before_the_run(self):
        """A container left running by a killed earlier run is found by its label and killed before this run starts, and the run uses --init so a kill reaches it."""
        with tempfile.TemporaryDirectory() as tmp:
            data = make_fixture(tmp)
            record = Path(tmp) / "record.txt"
            script = '#!/bin/bash\necho "$*" >> "{r}"\n[ "$1" = image ] && exit 0\nif [ "$1" = ps ]; then echo leftover123; exit 0; fi\n[ "$1" = kill ] && exit 0\nexit 1\n'
            bin_dir = make_docker(tmp, script.replace("{r}", str(record)))
            run_wrapper(REPO, Path(tmp) / "out", data=data, bin_dir=bin_dir, lock=Path(tmp) / "lock")
            calls = record.read_text().splitlines()
        verbs = [call.split()[0] for call in calls]
        self.assertLess(verbs.index("ps"), verbs.index("run"))
        self.assertIn("label=predbat-load-ml-bench", calls[verbs.index("ps")])
        self.assertEqual(calls[verbs.index("kill")], "kill leftover123")
        self.assertLess(verbs.index("kill"), verbs.index("run"))
        run = calls[verbs.index("run")].split()
        self.assertIn("--init", run)
        self.assertIn("predbat-load-ml-bench", " ".join(run))

    def test_a_second_run_waits_for_the_lock(self):
        """While another run holds the lock, the wrapper says it is waiting and does not start its own container."""
        import subprocess as sp
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            data = make_fixture(tmp)
            lock = Path(tmp) / "lock"
            holder = sp.Popen([sys.executable, "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'a')\nfcntl.flock(f, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(1.5)\n", str(lock)], stdout=sp.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            bin_dir = make_docker(tmp, "#!/bin/sh\nexit 1\n")
            code, output = run_wrapper(REPO, Path(tmp) / "out", data=data, bin_dir=bin_dir, lock=lock)
            holder.wait()
        self.assertIn("Waiting for the benchmark lock", output)

    def test_memory_benchmark_is_unchanged(self):
        """The memory benchmark takes no lock and passes no driver arguments: its runs and figures stay as they were."""
        text = MEMORY_WRAPPER.read_text()
        self.assertNotIn("bench_lock", text)
        self.assertNotIn("BENCH_DRIVER_ARGS", text)


if __name__ == "__main__":
    unittest.main()
