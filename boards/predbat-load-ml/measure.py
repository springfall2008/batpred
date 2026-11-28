#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""The measurement for board predbat-load-ml, run by the board runner as its [measure] command.

The runner runs it from the main checkout with BOARD_CANDIDATE set to the branch or commit the lead proposes. It checks
out the base (BOARD_BASE, default main) and the candidate into temporary worktrees and runs the LoadML training
benchmark on each, interleaved for ROUNDS rounds. The candidate passes when its median training time and median peak
RSS are each at least TIME_CUT and PEAK_CUT below the base's, its median RSS after training is no more than AFTER_RISE
above, every run is ok, and every run trained the same model: the base's forecast digest (the 48 h forecast at 2 dp),
the same epochs and the same val_mae at 4 dp. Exit 0 means pass, 1 fail, and 2 that the measurement itself could not be
made (the output then starts with HARNESS ERROR, so the lead knows the candidate was never judged).

The benchmark is coverage/load_ml_benchmark <tree> <out dir>. It takes the machine-wide benchmark lock itself, so this
measurement queues behind any worker's run rather than overlapping it, and writes <out dir>/result.json.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median

TIME_CUT = 0.25  # the candidate's median training_seconds must be at least this fraction below the base's
PEAK_CUT = 0.25  # and its median peak_rss_mb at least this fraction below
AFTER_RISE = 0.10  # and its median rss_after_mb no more than this fraction above
ROUNDS = 3  # training is deterministic, so few rounds are needed; the medians only smooth timing noise
VAL_MAE_DP = 4
BENCHMARK = Path("coverage/load_ml_benchmark")
NUMBER = (int, float)
FIELDS = {"ok": bool, "training_seconds": NUMBER, "peak_rss_mb": NUMBER, "rss_after_mb": NUMBER, "forecast_digest": str, "epochs_trained": int, "val_mae": NUMBER}
FIGURES = ("training_seconds", "peak_rss_mb", "rss_after_mb")


class HarnessError(RuntimeError):
    """The measurement could not be made, so the candidate was never judged: main exits 2."""


def load_result(path):
    """Read one benchmark result.json and check it has every field the verdict reads, with real, non-negative figures.

    A run profiled with --profile or --tracemalloc carries the profiler's overhead, so it is a harness error too.
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
    for field in FIGURES + ("val_mae",):
        if not math.isfinite(data[field]) or data[field] < 0:
            raise HarnessError(f"{path}: field {field} is {data[field]}, not a finite figure of zero or more")
    if data.get("profiled") or data.get("tracemalloc"):
        raise HarnessError(f"{path} is from a profiled run, whose figures include the profiler's overhead")
    return data


def training_key(run):
    """Return what says which model a run trained: the forecast digest, the epochs and val_mae at VAL_MAE_DP."""
    return (run["forecast_digest"], run["epochs_trained"], round(run["val_mae"], VAL_MAE_DP))


def verdict(base_runs, candidate_runs):
    """Compare base and candidate runs. Returns (passed, report lines); raises HarnessError when the base disagrees with itself."""
    lines = []
    for name, runs in (("base", base_runs), ("candidate", candidate_runs)):
        for index, run in enumerate(runs, 1):
            lines.append(
                f"{name} run {index}: training {run['training_seconds']:.1f} s, peak {run['peak_rss_mb']:.1f} MB, after {run['rss_after_mb']:.1f} MB, "
                f"ok {run['ok']}, forecast {run['forecast_digest'][:12]}, epochs {run['epochs_trained']}, val_mae {run['val_mae']:.6f}"
            )

    base_keys = {training_key(run) for run in base_runs}
    if len(base_keys) != 1:
        print("\n".join(lines))
        raise HarnessError("the base trained a different model between its own runs (forecast, epochs or val_mae), so the benchmark is not deterministic")
    base_digest, base_epochs, base_mae = next(iter(base_keys))

    failures = []
    if not all(run["ok"] for run in base_runs + candidate_runs):
        failures.append("a run reported ok false")
    if any(run["forecast_digest"] != base_digest for run in candidate_runs):
        failures.append("the candidate's forecast differs from the base's at 2 dp")
    if any(run["epochs_trained"] != base_epochs for run in candidate_runs):
        failures.append("the candidate trained for a different number of epochs")
    if any(round(run["val_mae"], VAL_MAE_DP) != base_mae for run in candidate_runs):
        failures.append(f"the candidate's val_mae differs from the base's at {VAL_MAE_DP} dp")

    medians = {name: {field: median(run[field] for run in runs) for field in FIGURES} for name, runs in (("base", base_runs), ("candidate", candidate_runs))}
    base, candidate = medians["base"], medians["candidate"]
    time_cut = 1 - candidate["training_seconds"] / base["training_seconds"] if base["training_seconds"] > 0 else 0.0
    peak_cut = 1 - candidate["peak_rss_mb"] / base["peak_rss_mb"] if base["peak_rss_mb"] > 0 else 0.0
    # Rounded so exactly 10% passes
    after_rise = round(candidate["rss_after_mb"] / base["rss_after_mb"] - 1, 6) if base["rss_after_mb"] > 0 else 0.0
    lines.append(f"median training time: base {base['training_seconds']:.1f} s, candidate {candidate['training_seconds']:.1f} s, cut {time_cut * 100:.1f}% (goal {TIME_CUT * 100:.0f}%)")
    lines.append(f"median peak RSS: base {base['peak_rss_mb']:.1f} MB, candidate {candidate['peak_rss_mb']:.1f} MB, cut {peak_cut * 100:.1f}% (goal {PEAK_CUT * 100:.0f}%)")
    lines.append(f"median RSS after training: base {base['rss_after_mb']:.1f} MB, candidate {candidate['rss_after_mb']:.1f} MB, {after_rise * 100:+.1f}% (limit +{AFTER_RISE * 100:.0f}%)")
    if round(time_cut, 6) < TIME_CUT:
        failures.append(f"training time cut {time_cut * 100:.1f}% is below {TIME_CUT * 100:.0f}%")
    if round(peak_cut, 6) < PEAK_CUT:
        failures.append(f"peak RSS cut {peak_cut * 100:.1f}% is below {PEAK_CUT * 100:.0f}%")
    if after_rise > AFTER_RISE:
        failures.append(f"the RSS kept after training is {after_rise * 100:.1f}% above the base's, more than {AFTER_RISE * 100:.0f}%")

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
        work = Path(tempfile.mkdtemp(prefix="predbat-load-ml-measure-"))
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
