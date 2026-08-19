# fmt: off
"""
ML Training Performance Harness

Exercises the real training path against a JSON fixture captured from a live system, so
training can be profiled and regression-tested at production scale without a live Home
Assistant. The fixture holds the same five history channels the ML component persists -
90 days at 5-minute resolution - which yields the ~11,400 sample feature matrix of the
largest curriculum pass.

Run the benchmark directly for memory figures:
    cd coverage
    venv/bin/python -c "import sys; sys.path.insert(0,'../apps/predbat'); \
        from tests.test_ml_training_perf import benchmark; benchmark()"

Note when interpreting local numbers on macOS: Apple's allocator keeps freed large blocks
in a cache rather than returning them, so a multi-pass run appears to climb without ever
falling back. Set MallocLargeCache=0 in the environment to see the figure a Linux
production host would report.
"""

import gzip
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import resource
except ImportError:
    resource = None

import numpy as np

from load_predictor import LoadPredictor, TOTAL_FEATURES

FIXTURE = "cases/ml_history_fixture.json.gz"
CHANNELS = ("load", "pv", "temp", "import_rate", "export_rate")
# Fixed reference time so the fixture's minute offsets always land on the same wall clock
FIXTURE_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)

# Measuring the peak must not perturb what it measures. An earlier version of this harness
# shelled out to ps from a sampler thread on every reading, and the fork/exec traffic
# interleaved with training's own allocations badly enough to change the result - the same
# workload read 967MB with the sampler running and 474MB without. ru_maxrss is the kernel's
# own high-water mark, so it needs no sampling at all: read it once before and once after.


def peak_rss_mb():
    """Return this process's peak resident set size in MB.

    The value only ever rises, so the difference across a section of work is that section's
    contribution to the peak. ru_maxrss is bytes on macOS and kilobytes on Linux.
    """
    if resource is None:
        return 0.0
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1e6 if sys.platform == "darwin" else value / 1e3


def load_ml_history_fixture():
    """Load the captured history into the five {minute: value} dicts training consumes.

    Mirrors load_ml_component's array_to_dict: step i is minute i * step_minutes counting
    backwards from now, and zero entries are absent rather than stored as zero.
    """
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        fixture = json.load(fh)

    step = fixture["step_minutes"]
    channels = {}
    for name in CHANNELS:
        values = fixture["channels"][name]
        channels[name] = {index * step: value for index, value in enumerate(values) if value != 0.0}
    return channels


def _train_one_pass(predictor, channels, epochs):
    """Run a single training pass over the full fixture at the given epoch count."""
    return predictor.train(
        channels["load"],
        FIXTURE_NOW,
        pv_minutes=channels["pv"],
        temp_minutes=channels["temp"],
        import_rates=channels["import_rate"],
        export_rates=channels["export_rate"],
        is_initial=True,
        epochs=epochs,
        patience=epochs,
        validation_holdout_hours=24,
    )


def _measure_here(epochs):
    """Run one training pass in this process and return its measurements."""
    channels = load_ml_history_fixture()
    predictor = LoadPredictor(learning_rate=0.001)

    baseline = peak_rss_mb()
    started = time.time()
    val_mae = _train_one_pass(predictor, channels, epochs)
    elapsed = time.time() - started

    return {
        "val_mae": val_mae,
        "growth": max(peak_rss_mb() - baseline, 0.0),
        "samples": len(channels["load"]),
        "seconds": elapsed,
    }


def measure_training(epochs=2):
    """Measure one training pass in a fresh process, returning (val_mae, growth, samples, seconds).

    A fresh process is what makes the number trustworthy. ru_maxrss is a high-water mark that
    only rises, so measuring in a process that has already trained once reports almost no
    growth however much the pass allocates. Spawning also keeps the measurement out of the
    way of what it measures - unlike sampling, nothing runs alongside training.
    """
    script = "import json,sys; sys.path.insert(0, {predbat!r}); from tests.test_ml_training_perf import _measure_here; print(json.dumps(_measure_here({epochs})))".format(
        predbat=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        epochs=epochs,
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=os.getcwd())
    if result.returncode != 0:
        raise RuntimeError("measurement subprocess failed: {}".format(result.stderr[-2000:]))

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return payload["val_mae"], payload["growth"], payload["samples"], payload["seconds"]


def test_ml_training_runs_at_production_scale(my_predbat=None):
    """A training pass over the captured 90 day history trains and validates cleanly"""
    print("\n=== Testing training at production scale ===")
    failed = 0

    channels = load_ml_history_fixture()
    days = max(channels["load"]) / (24 * 60)
    print("fixture: {} load points spanning {:.1f} days".format(len(channels["load"]), days))

    if len(channels["load"]) < 10000:
        print("ERROR: fixture too small to reach production scale: {} points".format(len(channels["load"])))
        return failed + 1

    predictor = LoadPredictor(learning_rate=0.001)
    val_mae = _train_one_pass(predictor, channels, epochs=2)

    if val_mae is None:
        print("ERROR: training returned no validation result")
        return failed + 1
    print("✓ training completed, val_mae {:.6f} kWh".format(val_mae))

    if not np.isfinite(val_mae) or val_mae < 0:
        print("ERROR: val_mae is not a sane value: {}".format(val_mae))
        failed += 1
    else:
        print("✓ val_mae is finite and non-negative")

    for index, weights in enumerate(predictor.weights):
        if not np.all(np.isfinite(weights)):
            print("ERROR: layer {} weights contain non-finite values after training".format(index))
            failed += 1
            break
    else:
        print("✓ all layers finite after training")

    if predictor.feature_mean is None or predictor.feature_mean.dtype != np.float32:
        print("ERROR: normalisation stats missing or not float32")
        failed += 1
    else:
        print("✓ normalisation stats fitted as float32")

    return failed


def test_training_does_not_copy_the_dataset_per_epoch(my_predbat=None):
    """Training must not hold the feature matrix it would otherwise materialise

    Every feature row is five sliding windows over five short channels plus time features,
    so consecutive rows overlap in all but one value and a materialised matrix stores each
    reading roughly LOOKBACK_STEPS times over. Assembling each batch from window views
    keeps the working set to a batch. The budget is expressed against the size that matrix
    would have been, so it holds whatever history the fixture carries.
    """
    print("\n=== Testing training working set ===")
    failed = 0

    val_mae, growth, samples, elapsed = measure_training(epochs=2)
    matrix_mb = samples * TOTAL_FEATURES * 4 / 1e6
    # Materialising the matrix costs it once for the dataset and again for the epoch
    # gather, which put growth above 2x. Assembling per batch leaves the optimiser state
    # and gradients as the largest items, well under it.
    budget = matrix_mb * 2.0

    print("notional matrix ~{:.1f} MB, growth {:.1f} MB ({:.2f}x), {:.1f}s".format(matrix_mb, growth, growth / matrix_mb, elapsed))

    if growth > budget:
        print("ERROR: growth {:.1f} MB exceeds {:.1f} MB ({:.1f}x the notional matrix) - the matrix is being materialised".format(growth, budget, budget / matrix_mb))
        failed += 1
    else:
        print("✓ working set within {:.1f}x the notional matrix".format(budget / matrix_mb))

    if val_mae is None or not np.isfinite(val_mae):
        print("ERROR: training did not produce a usable result: {}".format(val_mae))
        failed += 1
    else:
        print("✓ training still produced a valid result")

    return failed


def benchmark(epochs=2):
    """Report footprint growth for one production-scale training pass."""
    val_mae, growth, samples, elapsed = measure_training(epochs=epochs)
    matrix_mb = samples * TOTAL_FEATURES * 4 / 1e6
    print("epochs {}  val_mae {}  {:.1f}s".format(epochs, val_mae, elapsed))
    print("dataset ~{:.1f} MB   growth {:.1f} MB   ({:.2f}x dataset)".format(matrix_mb, growth, growth / matrix_mb))
    return growth


def run_ml_training_perf_tests(my_predbat=None):
    """Run all ML training performance harness tests"""
    print("\n" + "=" * 80)
    print("ML Training Performance Tests")
    print("=" * 80)

    failed = 0
    failed += test_ml_training_runs_at_production_scale(my_predbat)
    failed += test_training_does_not_copy_the_dataset_per_epoch(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All ML training performance tests passed!")
    else:
        print(f"❌ {failed} ML training performance test(s) failed")
    print("=" * 80)

    return failed
