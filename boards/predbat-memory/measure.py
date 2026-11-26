#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""The measurement for board predbat-memory, run by the board runner as its [measure] command.

The runner runs it from the main checkout with BOARD_CANDIDATE set to the branch or commit the lead proposes. It checks
out the base (BOARD_BASE, default main) and the candidate into temporary worktrees, runs the memory benchmark on each,
interleaved for ROUNDS rounds, and compares the median steady-state RSS against the goal (the median peak may rise by no
more than PEAK_RISE), with every plan_digest equal to the base's. Exit 0 means the candidate meets the goal,
1 means it does not, and 2 means the measurement itself could not be made (the runner counts that as a fail, so the
output starts with HARNESS ERROR to tell the lead the candidate was never judged).

The benchmark is coverage/memory_benchmark <tree> <out dir>. It runs Predbat standalone from <tree> (db_primary, the
dummy inverter, a fixed run of plan cycles, in a Linux container) and writes <out dir>/result.json:

    {"ok": true, "steady_rss_mb": 120.5, "peak_rss_mb": 150.2, "plan_digest": "<hex>"}
"""

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median

STEADY_CUT = 0.25  # the candidate's median steady-state RSS must be at least this fraction below the base's
PEAK_RISE = 0.10  # and its median peak RSS no more than this fraction above the base's
ROUNDS = 3
BENCHMARK = Path("coverage/memory_benchmark")
FIELDS = {"ok": bool, "steady_rss_mb": (int, float), "peak_rss_mb": (int, float), "plan_digest": str}
FIGURES = ("steady_rss_mb", "peak_rss_mb")


class HarnessError(RuntimeError):
    """The measurement could not be made, so the candidate was never judged: main exits 2."""


def load_result(path):
    """Read one benchmark result.json and check it has every field with the right type and real, non-negative figures.

    json.loads accepts the NaN and Infinity that json.dump writes by default, and every comparison in verdict is false
    against NaN, so a figure that is not finite would pass; a run profiled with --tracemalloc carries tracemalloc's own
    overhead in its RSS. Both are harness errors.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise HarnessError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessError(f"{path} is not a JSON object")
    for field, kind in FIELDS.items():
        value = data.get(field)
        if isinstance(value, bool) and kind is not bool:
            value = None
        if not isinstance(value, kind):
            raise HarnessError(f"{path}: field {field} is missing or not {kind}")
    for field in FIGURES:
        if not math.isfinite(data[field]) or data[field] < 0:
            raise HarnessError(f"{path}: field {field} is {data[field]}, not a finite figure of zero or more")
    if data.get("tracemalloc"):
        raise HarnessError(f"{path} is from a --tracemalloc run, whose RSS includes tracemalloc's overhead")
    return data


def verdict(base_runs, candidate_runs):
    """Compare base and candidate runs. Returns (passed, report lines); raises HarnessError when the base disagrees with itself."""
    lines = []
    for name, runs in (("base", base_runs), ("candidate", candidate_runs)):
        for index, run in enumerate(runs, 1):
            lines.append(f"{name} run {index}: steady {run['steady_rss_mb']:.1f} MB, peak {run['peak_rss_mb']:.1f} MB, ok {run['ok']}, plan {run['plan_digest']}")

    base_digests = {run["plan_digest"] for run in base_runs}
    if len(base_digests) != 1:
        print("\n".join(lines))
        raise HarnessError("the base's plan_digest differs between its own runs, so the benchmark is not deterministic")

    failures = []
    if not all(run["ok"] for run in base_runs + candidate_runs):
        failures.append("a run reported ok false")
    if {run["plan_digest"] for run in candidate_runs} != base_digests:
        failures.append("the candidate's plan_digest differs from the base's: plans changed")

    base_steady = median(run["steady_rss_mb"] for run in base_runs)
    candidate_steady = median(run["steady_rss_mb"] for run in candidate_runs)
    base_peak = median(run["peak_rss_mb"] for run in base_runs)
    candidate_peak = median(run["peak_rss_mb"] for run in candidate_runs)
    cut = 1 - candidate_steady / base_steady if base_steady > 0 else 0.0
    lines.append(f"median steady: base {base_steady:.1f} MB, candidate {candidate_steady:.1f} MB, cut {cut * 100:.1f}% (goal {STEADY_CUT * 100:.0f}%)")
    # The goal is steady-state memory; the peak only has to stay within PEAK_RISE of the base's, far above its 1 to 2 MB
    # of run-to-run noise, so a steady saving is never bought with a much higher peak. Rounded so exactly 10% passes
    rise = round(candidate_peak / base_peak - 1, 6) if base_peak > 0 else 0.0
    lines.append(f"median peak: base {base_peak:.1f} MB, candidate {candidate_peak:.1f} MB, {rise * 100:+.1f}% (limit +{PEAK_RISE * 100:.0f}%)")
    if cut < STEADY_CUT:
        failures.append(f"steady-state cut {cut * 100:.1f}% is below {STEADY_CUT * 100:.0f}%")
    if rise > PEAK_RISE:
        failures.append(f"the candidate's median peak RSS is {rise * 100:.1f}% above the base's, more than {PEAK_RISE * 100:.0f}%")

    lines.append("RESULT: pass" if not failures else "RESULT: fail - " + "; ".join(failures))
    return not failures, lines


def _git(*args, cwd=None):
    """Run git and return its stripped stdout; raise HarnessError on a non-zero exit."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HarnessError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _run_benchmark(benchmark, tree, out_dir):
    """Run the benchmark on tree into out_dir and return its checked result; any failure to run it is a HarnessError."""
    try:
        out_dir.mkdir(parents=True)
        with (out_dir / "benchmark.log").open("w") as log:
            proc = subprocess.run([str(benchmark), str(tree), str(out_dir)], stdout=log, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise HarnessError(f"cannot run {benchmark} for {tree}: {exc}") from exc
    if proc.returncode != 0:
        raise HarnessError(f"benchmark exited {proc.returncode} for {tree}; see {out_dir / 'benchmark.log'}")
    return load_result(out_dir / "result.json")


def main():
    """Measure BOARD_CANDIDATE against BOARD_BASE and return the exit code: 0 pass, 1 fail, 2 harness error."""
    candidate = os.environ.get("BOARD_CANDIDATE", "").strip()
    base = os.environ.get("BOARD_BASE", "main").strip()
    if not candidate:
        print("HARNESS ERROR: BOARD_CANDIDATE is not set")
        return 2
    try:
        root = Path(_git("rev-parse", "--show-toplevel"))
        benchmark = root / BENCHMARK
        if not os.access(benchmark, os.X_OK):
            raise HarnessError(f"{BENCHMARK} is missing or not executable: the benchmark has not been built yet")
        work = Path(tempfile.mkdtemp(prefix="predbat-memory-measure-"))
        trees = {}
        try:
            for name, ref in (("base", base), ("candidate", candidate)):
                commit = _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=root)
                print(f"{name}: {ref} at {commit}")
                trees[name] = work / name
                _git("worktree", "add", "--detach", str(trees[name]), commit, cwd=root)
            runs = {"base": [], "candidate": []}
            for index in range(ROUNDS):
                order = ("base", "candidate") if index % 2 == 0 else ("candidate", "base")
                for name in order:
                    runs[name].append(_run_benchmark(benchmark, trees[name], work / "out" / f"{name}-{index + 1}"))
            passed, lines = verdict(runs["base"], runs["candidate"])
            print("\n".join(lines))
            print(f"logs: {work / 'out'}")
            return 0 if passed else 1
        finally:
            for tree in trees.values():
                subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=root, capture_output=True)
    except HarnessError as exc:
        print(f"HARNESS ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
