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

Note when interpreting the memory figure anywhere: ru_maxrss is a high-water mark of pages
ever touched, not memory held at any instant. Training frees each batch buffer immediately -
it retains ~0MB of live arrays and peaks at ~67MB of Python-tracked memory - yet the mark
accumulates every page those thousands of short-lived buffers ever dirtied, reporting ~280MB.
It is also page-granular, so a 16KB-page host (Apple Silicon) inflates it several-fold
against a 4KB-page Linux one. The figure is therefore useful for comparing runs on one
machine and meaningless as an absolute or across machines. (MallocLargeCache=0 is sometimes
suggested for this on macOS; measured here it changes nothing, because allocator caching is
not what the mark is recording.)
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


def _measure_allocations_here(epochs):
    """Run one training pass in this process, watching for a materialised matrix.

    Traps WindowedFeatures.__array__ (the only path that builds every row at once) and the
    largest numpy allocation made during training, both of which are properties of the code
    rather than of the host's page size or allocator.
    """
    import load_predictor

    counter = {"array": 0, "largest": 0}

    original_array = load_predictor.WindowedFeatures.__array__

    def counting_array(self, dtype=None, copy=None):
        counter["array"] += 1
        return original_array(self, dtype, copy)

    load_predictor.WindowedFeatures.__array__ = counting_array

    # np.empty/np.zeros cover how this module allocates; a view or slice allocates nothing
    original_empty, original_zeros = np.empty, np.zeros

    def watch(factory):
        def wrapper(shape, *args, **kwargs):
            result = factory(shape, *args, **kwargs)
            if result.nbytes > counter["largest"]:
                counter["largest"] = result.nbytes
            return result

        return wrapper

    np.empty, np.zeros = watch(original_empty), watch(original_zeros)
    try:
        channels = load_ml_history_fixture()
        predictor = LoadPredictor(learning_rate=0.001)
        baseline = peak_rss_mb()
        started = time.time()
        val_mae = _train_one_pass(predictor, channels, epochs)
        elapsed = time.time() - started
        growth = max(peak_rss_mb() - baseline, 0.0)
    finally:
        np.empty, np.zeros = original_empty, original_zeros
        load_predictor.WindowedFeatures.__array__ = original_array

    return {
        "materialised": counter["array"],
        "largest_mb": counter["largest"] / 1e6,
        "val_mae": val_mae,
        "growth": growth,
        "samples": len(channels["load"]),
        "seconds": elapsed,
    }


def measure_training_allocations(epochs=2):
    """Measure one training pass in a fresh process, returning its allocation behaviour.

    A fresh process keeps the measurement clear of anything an earlier test allocated, and
    keeps the numpy patching out of the rest of the suite.
    """
    payload = _run_in_fresh_process("_measure_allocations_here", epochs)
    return payload["materialised"], payload["largest_mb"], payload["val_mae"], payload["growth"], payload["samples"], payload["seconds"]


def _run_in_fresh_process(entry, epochs):
    """Run one of this module's measurement entry points in a subprocess and return its payload."""
    script = "import json,sys; sys.path.insert(0, {predbat!r}); from tests.test_ml_training_perf import {entry}; print(json.dumps({entry}({epochs})))".format(
        predbat=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        entry=entry,
        epochs=epochs,
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=os.getcwd())
    if result.returncode != 0:
        raise RuntimeError("measurement subprocess failed: {}".format(result.stderr[-2000:]))
    return json.loads(result.stdout.strip().splitlines()[-1])


def measure_training(epochs=2):
    """Measure one training pass in a fresh process, returning (val_mae, growth, samples, seconds).

    Used by benchmark() for the raw footprint figure. A fresh process is what makes the number
    trustworthy: ru_maxrss only rises, so measuring in a process that has already trained once
    reports almost no growth however much the pass allocates. Note the figure is a high-water
    mark of pages ever touched and is page-granular, so it is much larger than the memory held
    at any instant and is not comparable across hosts with different page sizes - see
    test_training_does_not_copy_the_dataset_per_epoch.
    """
    payload = _run_in_fresh_process("_measure_here", epochs)
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
    """Training must not materialise the feature matrix it assembles per batch

    Every feature row is five sliding windows over five short channels plus time features,
    so consecutive rows overlap in all but one value and a materialised matrix stores each
    reading roughly LOOKBACK_STEPS times over. Assembling each batch from window views
    keeps the working set to a batch.

    This asserts the invariant directly - that WindowedFeatures.__array__ is never reached
    and that no single allocation approaches the matrix - rather than watching peak RSS.
    ru_maxrss is a high-water mark of pages *ever touched*, not memory concurrently held,
    so thousands of short-lived batch buffers accumulate into it even though each is freed
    immediately: measured here, training peaks at 67MB of Python-tracked memory and retains
    0.0MB of live arrays, while ru_maxrss reports ~280MB. The mark is also page-granular,
    so a 16KB-page host (Apple Silicon) inflates it several-fold against a 4KB-page one,
    which made the previous RSS budget fail everywhere on macOS and never run in CI at all
    (it is a slow test, and CI runs --quick). RSS is still reported below as a diagnostic,
    just not asserted on.
    """
    print("\n=== Testing training working set ===")
    failed = 0

    materialised, largest_mb, val_mae, growth, samples, elapsed = measure_training_allocations(epochs=2)
    matrix_mb = samples * TOTAL_FEATURES * 4 / 1e6

    print("notional matrix ~{:.1f} MB, largest single allocation {:.1f} MB, peak RSS growth {:.1f} MB (not asserted), {:.1f}s".format(matrix_mb, largest_mb, growth, elapsed))

    if materialised:
        print("ERROR: WindowedFeatures.__array__ was called {} time(s) during training - the matrix is being materialised".format(materialised))
        failed += 1
    else:
        print("✓ the full matrix is never materialised")

    # Any single allocation at matrix scale means a row-per-sample array was built somewhere
    # __array__ does not cover. A batch is orders of magnitude smaller than the matrix.
    if largest_mb > matrix_mb / 2:
        print("ERROR: a single allocation of {:.1f} MB approaches the {:.1f} MB matrix".format(largest_mb, matrix_mb))
        failed += 1
    else:
        print("✓ no single allocation approaches the notional matrix")

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
