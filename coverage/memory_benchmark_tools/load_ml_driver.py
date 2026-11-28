# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""The LoadML training benchmark's driver, run inside the container by container_run.sh (BENCH_DRIVER).

    python3 load_ml_driver.py --out /out [--timeout S] [--profile | --tracemalloc]

Patches LoadMLComponent.run on the class before Predbat is built, so every tick of the component is metered from the
first, then starts Predbat through the same path driver.py takes, from a fixture with LoadML on, its 90-day history
and no saved model. It runs no plan cycles: it waits until a tick has completed the initial training, adding up the wall
and CPU time of every LoadML tick to that point (not the idle gap between ticks), with the peak RSS reset as the first
tick starts. It writes <out>/result.json, forecast.json (the trained model's 48 h forecast at 2 dp) and modules.txt;
--profile adds profile.txt (cProfile over the ticks) and --tracemalloc adds tracemalloc.txt, either marking the result
as profiled.
"""

import argparse
import hashlib
import json
import os
import pstats
import sys
import threading
import time
import traceback
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from driver import ALLOWED_WARNINGS, BENIGN_WARNINGS, READY_COMPONENTS, check_log, finish_profile, read_status_kb, wait_ready, write_module_report  # noqa: E402

# Warnings LoadML is known to log in a clean offline benchmark run, each with the reason it is harmless here. Empty on
# purpose: the clean runs so far logged none, so any LoadML warning is a problem until a real run shows one is harmless
LOAD_ML_BENIGN_WARNINGS = {}

PROFILE_TOP = 40


def dp2_forecast(predictions):
    """Return a {minute: cumulative kWh} forecast with string keys and each value rounded to 2 dp."""
    return {str(minute): round(float(value), 2) for minute, value in (predictions or {}).items()}


def forecast_digest(predictions):
    """Return the sha256 hex digest of the forecast at 2 dp, independent of dict order."""
    return hashlib.sha256(json.dumps(dp2_forecast(predictions), sort_keys=True).encode()).hexdigest()


def check_load_ml_log(text):
    """Return the problems in a predbat.log: the memory benchmark's rules, also allowing LoadML's harmless warnings when any are listed."""
    return check_log(text, ALLOWED_WARNINGS, {**BENIGN_WARNINGS, **LOAD_ML_BENIGN_WARNINGS})


def reset_peak(clear_refs="/proc/self/clear_refs"):
    """Reset the process's VmHWM to its current RSS (Linux: write 5 to clear_refs); return False where that is refused."""
    try:
        with open(clear_refs, "w") as handle:
            handle.write("5")
        return True
    except OSError:
        return False


class PeakSampler:
    """Samples RSS on a daemon thread and keeps the highest reading: the peak where VmHWM cannot be reset."""

    def __init__(self, read_rss, interval=0.05):
        self.read_rss = read_rss
        self.interval = interval
        self.peak_kb = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        """Read RSS every interval until stopped."""
        while not self._stop.is_set():
            self.peak_kb = max(self.peak_kb, self.read_rss() or 0)
            self.samples += 1
            self._stop.wait(self.interval)

    def start(self):
        """Start sampling."""
        self._thread.start()

    def stop(self):
        """Stop sampling and return the peak in kB."""
        self._stop.set()
        self._thread.join()
        return self.peak_kb


class TickMeter:
    """Meters the LoadML ticks up to and including the one that completes the initial training.

    begin() and end() bracket each tick. The first begin() records the RSS and resets the peak (or, where that is
    refused, starts a sampler); each end() adds the tick's wall and CPU time; the end() of the training tick records the
    peak and the RSS after, and sets finished. Ticks after that are ignored.
    """

    def __init__(self, wall=time.monotonic, cpu=time.process_time, read_rss_kb=None, read_hwm_kb=None, reset=reset_peak, sampler_factory=PeakSampler, profiler=None):
        self.wall = wall
        self.cpu = cpu
        self.read_rss_kb = read_rss_kb or (lambda: read_status_kb("VmRSS"))
        self.read_hwm_kb = read_hwm_kb or (lambda: read_status_kb("VmHWM"))
        self.reset = reset
        self.sampler_factory = sampler_factory
        self.profiler = profiler
        self.ticks = 0
        self.wall_seconds = 0.0
        self.cpu_seconds = 0.0
        self.training_tick_seconds = None
        self.rss_before_kb = None
        self.peak_kb = None
        self.rss_after_kb = None
        self.peak_method = None
        self.done = False
        self.finished = threading.Event()
        self._sampler = None
        self._tick_wall = None
        self._tick_cpu = None

    def begin(self):
        """Start metering a tick."""
        if self.done:
            return
        if self.ticks == 0 and self.peak_method is None:
            self.rss_before_kb = self.read_rss_kb()
            if self.reset():
                self.peak_method = "clear_refs"
            else:
                self.peak_method = "sampler"
                self._sampler = self.sampler_factory(self.read_rss_kb)
                self._sampler.start()
        self._tick_wall = self.wall()
        self._tick_cpu = self.cpu()
        if self.profiler:
            self.profiler.enable()

    def end(self, trained):
        """Finish metering a tick; trained says whether the initial training is now complete."""
        if self.done or self._tick_wall is None:
            return
        if self.profiler:
            self.profiler.disable()
        tick = self.wall() - self._tick_wall
        self.wall_seconds += tick
        self.cpu_seconds += self.cpu() - self._tick_cpu
        self.ticks += 1
        self._tick_wall = None
        if trained:
            self.training_tick_seconds = tick
            self.peak_kb = self._sampler.stop() if self._sampler else self.read_hwm_kb()
            self.rss_after_kb = self.read_rss_kb()
            self.done = True
            self.finished.set()


def instrument(component_class, meter):
    """Wrap component_class.run so each tick is metered; return a function that puts the original back."""
    original = component_class.run

    async def run(self, seconds, first):
        """The component's own run(), bracketed by the meter."""
        meter.begin()
        try:
            return await original(self, seconds, first)
        finally:
            # Read directly: LoadMLComponent always sets it, and a rename must fail here, not read as "never trained" until the timeout
            meter.end(trained=bool(self.initial_training_done))

    component_class.run = run

    def restore():
        """Put the original run back."""
        component_class.run = original

    return restore


def _mb(kb):
    """Return kB as MiB to 1 dp, or 0.0 when unknown."""
    return round(kb / 1024.0, 1) if kb is not None else 0.0


def build_result(meter, component, problems, queued_seconds):
    """Return result.json's dict from the meter and the LoadML component; ok only for a clean run that trained."""
    problems = list(problems)
    predictions = getattr(component, "current_predictions", None) or {}
    predictor = getattr(component, "predictor", None)
    if not meter.done:
        problems.append("training never completed")
    elif getattr(component, "model_status", None) != "active":
        problems.append("model status after training is {}, not active".format(getattr(component, "model_status", None)))
    if meter.done and not predictions:
        problems.append("training produced no forecast")
    val_mae = getattr(predictor, "validation_mae", None)
    return {
        "ok": not problems,
        "training_seconds": round(meter.wall_seconds, 2),
        "training_cpu_seconds": round(meter.cpu_seconds, 2),
        "training_tick_seconds": round(meter.training_tick_seconds, 2) if meter.training_tick_seconds is not None else 0.0,
        "rss_before_mb": _mb(meter.rss_before_kb),
        "peak_rss_mb": _mb(meter.peak_kb),
        "rss_after_mb": _mb(meter.rss_after_kb),
        "forecast_digest": forecast_digest(predictions) if predictions else "",
        "val_mae": float(val_mae) if val_mae is not None else None,
        "epochs_trained": int(getattr(predictor, "epochs_trained", 0) or 0),
        "peak_method": meter.peak_method or "",
        "queued_seconds": float(queued_seconds),
        "ticks": meter.ticks,
        "problems": problems[:50],
    }


def write_profile(out, profiler, result):
    """Write <out>/profile.txt, the top functions by cumulative and by own time over the LoadML ticks; mark the result."""
    lines = []
    for title, key in (("by cumulative time", "cumulative"), ("by own time", "tottime")):
        buffer = StringIO()
        pstats.Stats(profiler, stream=buffer).sort_stats(key).print_stats(PROFILE_TOP)
        lines += ["Top {} functions {}:".format(PROFILE_TOP, title), buffer.getvalue(), ""]
    (Path(out) / "profile.txt").write_text("\n".join(lines))
    result["profiled"] = True


def run(out, timeout, profile, ready_timeout=180):
    """Run the benchmark and return the result dict."""
    from multiprocessing import set_start_method

    # predbat before load_ml_component, as hass.py imports it in production: its module-level setup (such as
    # OPENBLAS_THREAD_TIMEOUT, read once when NumPy loads OpenBLAS) must come before load_ml_component imports NumPy
    import predbat
    import load_ml_component

    profiler = None
    if profile:
        import cProfile

        profiler = cProfile.Profile()
    meter = TickMeter(profiler=profiler)
    instrument(load_ml_component.LoadMLComponent, meter)

    set_start_method("fork")
    problems = []
    began = time.monotonic()
    my_predbat = predbat.PredBat()
    my_predbat.initialize()
    startup_seconds = round(time.monotonic() - began, 1)
    waiting = wait_ready(my_predbat, READY_COMPONENTS, ready_timeout)
    if waiting:
        problems.append("components never published: {}".format(", ".join(waiting)))
    component = my_predbat.components.get_component("load_ml")
    if component is None:
        problems.append("the load_ml component is not running (is load_ml_enable set in the fixture?)")
    elif not meter.finished.wait(timeout):
        problems.append("training did not finish within {:g} s".format(timeout))
    log_path = Path("predbat.log")
    problems += check_load_ml_log(log_path.read_text(errors="replace")) if log_path.exists() else ["no predbat.log"]
    result = build_result(meter, component, problems, float(os.environ.get("BENCH_QUEUED_SECONDS", 0) or 0))
    result["startup_seconds"] = startup_seconds
    (Path(out) / "forecast.json").write_text(json.dumps(dp2_forecast(getattr(component, "current_predictions", None)), indent=1, sort_keys=True))
    write_module_report(out, result)
    if profiler:
        write_profile(out, profiler, result)
    finish_profile(out, result)
    return result


def parse_args(argv=None):
    """Parse the driver's command line."""
    parser = argparse.ArgumentParser(description="LoadML training benchmark driver (runs inside the benchmark container)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cycles", type=int, default=0, help="accepted for container_run.sh, which always passes it; no plan cycles are run")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds to wait for the initial training to finish")
    parser.add_argument("--ready-timeout", type=float, default=180)
    parser.add_argument("--profile", action="store_true", help="cProfile the LoadML ticks and write profile.txt")
    parser.add_argument("--tracemalloc", action="store_true", help="trace allocations from before Predbat is imported and write tracemalloc.txt")
    return parser.parse_args(argv)


def main(argv=None):
    """Run the benchmark, write <out>/result.json and exit; 0 when the result was written."""
    args = parse_args(argv)
    if args.tracemalloc:
        import tracemalloc

        # Started before predbat is imported, one frame per allocation, as driver.py does
        tracemalloc.start(1)
    try:
        result = run(Path(args.out), args.timeout, args.profile, args.ready_timeout)
    except Exception:
        result = {"ok": False, "training_seconds": 0.0, "peak_rss_mb": 0.0, "rss_after_mb": 0.0, "forecast_digest": "", "problems": ["driver raised: " + traceback.format_exc()]}
    (Path(args.out) / "result.json").write_text(json.dumps(result, indent=2))
    sys.stdout.flush()
    # Component threads are still running; leave without waiting for them
    os._exit(0)


if __name__ == "__main__":
    main()
