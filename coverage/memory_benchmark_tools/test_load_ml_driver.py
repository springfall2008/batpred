# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the LoadML training benchmark's driver, with fake clocks and a fake component: no Predbat is started."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import load_ml_driver  # noqa: E402


class FakeClock:
    """A clock that returns the next value of a list each time it is read."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        """Return the next reading."""
        return self.values.pop(0)


class FakeSampler:
    """Stands in for PeakSampler: remembers that it was started and returns a fixed peak."""

    def __init__(self, read_rss, peak_kb=300 * 1024):
        self.started = False
        self.peak_kb = peak_kb

    def start(self):
        """Note the start."""
        self.started = True

    def stop(self):
        """Return the fixed peak."""
        return self.peak_kb


def make_meter(wall, cpu, reset=lambda: True, hwm_kb=250 * 1024, rss_kb=(100 * 1024, 120 * 1024)):
    """A TickMeter on fake clocks; reset says whether clear_refs works, and the RSS readings are before then after."""
    rss = list(rss_kb)
    resets = []

    def do_reset():
        resets.append(True)
        return reset()

    meter = load_ml_driver.TickMeter(wall=FakeClock(*wall), cpu=FakeClock(*cpu), read_rss_kb=lambda: rss.pop(0), read_hwm_kb=lambda: hwm_kb, reset=do_reset, sampler_factory=FakeSampler)
    return meter, resets


class FakePredictor:
    """The predictor attributes the result reads."""

    validation_mae = 0.0184329
    epochs_trained = 900


class FakeComponent:
    """Just enough of LoadMLComponent for the driver: run() trains on its second tick."""

    def __init__(self):
        self.ticks = 0
        self.initial_training_done = False
        self.model_status = "not_initialized"
        self.current_predictions = {}
        self.predictor = FakePredictor()

    async def run(self, seconds, first):
        """Defer on the first tick, train on the second."""
        self.ticks += 1
        if self.ticks == 2:
            self.initial_training_done = True
            self.model_status = "active"
            self.current_predictions = {5: 0.1234, 10: 0.2468}
        return "tick {}".format(self.ticks)


class TestForecastDigest(unittest.TestCase):
    """The forecast digest compares forecasts at 2 dp, whatever the dict order."""

    def test_forecast_digest_rounds_to_dp2_and_ignores_key_order(self):
        """Values that round to the same 2 dp give one digest; a 0.01 change gives another."""
        one = {5: 1.231, 10: 2.5}
        two = {10: 2.5, 5: 1.229}
        self.assertEqual(load_ml_driver.forecast_digest(one), load_ml_driver.forecast_digest(two))
        self.assertNotEqual(load_ml_driver.forecast_digest(one), load_ml_driver.forecast_digest({5: 1.24, 10: 2.5}))
        self.assertEqual(load_ml_driver.dp2_forecast(one), {"5": 1.23, "10": 2.5})


class TestTickMeter(unittest.TestCase):
    """The meter adds up the LoadML ticks up to the one that trains, and nothing after."""

    def test_tick_meter_sums_ticks_and_skips_the_gap(self):
        """Tick 1 runs 0-10 s, tick 2 (training) 70-370 s: 310 s counted, the 60 s gap not, 300 s for the training tick."""
        meter, _ = make_meter(wall=[0, 10, 70, 370], cpu=[0, 5, 5, 1205])
        meter.begin()
        meter.end(trained=False)
        meter.begin()
        meter.end(trained=True)
        self.assertEqual(meter.wall_seconds, 310)
        self.assertEqual(meter.cpu_seconds, 1205)
        self.assertEqual(meter.training_tick_seconds, 300)
        self.assertTrue(meter.done)
        self.assertTrue(meter.finished.is_set())
        self.assertEqual((meter.rss_before_kb, meter.peak_kb, meter.rss_after_kb), (100 * 1024, 250 * 1024, 120 * 1024))

    def test_tick_meter_resets_the_peak_once_before_the_first_tick(self):
        """clear_refs is written once, as the first tick starts, so startup's own peak is left out."""
        meter, resets = make_meter(wall=[0, 1, 2, 3], cpu=[0, 1, 2, 3])
        meter.begin()
        meter.end(trained=False)
        meter.begin()
        meter.end(trained=True)
        self.assertEqual(resets, [True])
        self.assertEqual(meter.peak_method, "clear_refs")

    def test_tick_meter_falls_back_to_the_sampler_when_reset_fails(self):
        """Where the kernel refuses clear_refs the peak comes from a sampler started at the first tick."""
        meter, _ = make_meter(wall=[0, 1], cpu=[0, 1], reset=lambda: False)
        meter.begin()
        meter.end(trained=True)
        self.assertEqual(meter.peak_method, "sampler")
        self.assertEqual(meter.peak_kb, 300 * 1024)

    def test_tick_meter_ignores_ticks_after_training(self):
        """Once training has finished, later ticks change nothing (and read no clock)."""
        meter, _ = make_meter(wall=[0, 5], cpu=[0, 5])
        meter.begin()
        meter.end(trained=True)
        meter.begin()
        meter.end(trained=True)
        self.assertEqual(meter.wall_seconds, 5)


class TestInstrument(unittest.TestCase):
    """instrument() wraps the component class's run so every tick is metered."""

    def test_instrument_wraps_and_restores_run(self):
        """run's result passes through, the training tick ends the meter, and restore puts the original back."""
        original = FakeComponent.run
        meter, _ = make_meter(wall=[0, 1, 2, 4], cpu=[0, 1, 2, 4])
        restore = load_ml_driver.instrument(FakeComponent, meter)
        try:
            component = FakeComponent()
            self.assertEqual(asyncio.run(component.run(0, True)), "tick 1")
            self.assertFalse(meter.done)
            self.assertEqual(asyncio.run(component.run(60, False)), "tick 2")
            self.assertTrue(meter.done)
            self.assertEqual(meter.wall_seconds, 3)
        finally:
            restore()
        self.assertIs(FakeComponent.run, original)

    def test_instrument_ends_the_tick_when_run_raises(self):
        """A tick that raises is still metered, and the exception reaches the caller."""

        class Broken(FakeComponent):
            async def run(self, seconds, first):
                """Fail."""
                raise RuntimeError("boom")

        meter, _ = make_meter(wall=[0, 2], cpu=[0, 2])
        restore = load_ml_driver.instrument(Broken, meter)
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(Broken().run(0, True))
        finally:
            restore()
        self.assertEqual(meter.wall_seconds, 2)

    def test_instrument_fails_loudly_without_the_training_flag(self):
        """A component without initial_training_done raises at once, rather than read as never trained until the timeout."""

        class Renamed:
            """A component whose training flag has been renamed."""

            async def run(self, seconds, first):
                """Tick."""
                return "tick"

        meter, _ = make_meter(wall=[0, 1], cpu=[0, 1])
        restore = load_ml_driver.instrument(Renamed, meter)
        try:
            with self.assertRaises(AttributeError):
                asyncio.run(Renamed().run(0, True))
        finally:
            restore()


class TestBuildResult(unittest.TestCase):
    """The result is ok only for a clean run whose training finished with an active model and a forecast."""

    def trained(self):
        """A meter and component after a successful training tick."""
        meter, _ = make_meter(wall=[0, 300.456], cpu=[0, 1200.123])
        component = FakeComponent()
        asyncio.run(component.run(0, True))
        asyncio.run(component.run(60, False))
        meter.begin()
        meter.end(trained=True)
        return meter, component

    def test_build_result_ok_only_when_trained_active_and_forecast_present(self):
        """Each missing condition gives ok false with a named problem."""
        meter, component = self.trained()
        result = load_ml_driver.build_result(meter, component, [], 12.0)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["training_seconds"], 300.46)
        self.assertEqual(result["training_cpu_seconds"], 1200.12)
        self.assertEqual((result["rss_before_mb"], result["peak_rss_mb"], result["rss_after_mb"]), (100.0, 250.0, 120.0))
        self.assertEqual((result["val_mae"], result["epochs_trained"], result["queued_seconds"]), (0.0184329, 900, 12.0))
        self.assertEqual(result["forecast_digest"], load_ml_driver.forecast_digest(component.current_predictions))
        self.assertEqual(result["peak_method"], "clear_refs")

        self.assertFalse(load_ml_driver.build_result(meter, component, ["Error: x"], 0)["ok"])

        component.model_status = "fallback_validation"
        result = load_ml_driver.build_result(meter, component, [], 0)
        self.assertFalse(result["ok"])
        self.assertIn("model status after training is fallback_validation, not active", result["problems"])

        component.model_status = "active"
        component.current_predictions = {}
        result = load_ml_driver.build_result(meter, component, [], 0)
        self.assertIn("training produced no forecast", result["problems"])

        untrained, _ = make_meter(wall=[], cpu=[])
        result = load_ml_driver.build_result(untrained, FakeComponent(), [], 0)
        self.assertFalse(result["ok"])
        self.assertIn("training never completed", result["problems"])


class TestLog(unittest.TestCase):
    """The log check is the memory benchmark's, plus LoadML's own harmless warnings when any are listed."""

    def test_check_load_ml_log_reuses_the_memory_rules(self):
        """An Error line is a problem; a listed offline Octopus warning is not."""
        log = "2026-11-27 19:00:00.1: Error: something broke\n2026-11-27 19:00:01.0: Warn: OctopusAPI: Failed to retrieve auth token\n"
        self.assertEqual(load_ml_driver.check_load_ml_log(log), ["Error: something broke"])

    def test_a_listed_load_ml_warning_is_allowed(self):
        """LOAD_ML_BENIGN_WARNINGS is empty on purpose, but an entry added to it does allow its warning, and every entry gives a reason."""
        log = "2026-11-27 19:00:00.1: Warn: ML Component: something harmless offline\n"
        self.assertEqual(load_ml_driver.check_load_ml_log(log), ["Warn: ML Component: something harmless offline"])
        saved = dict(load_ml_driver.LOAD_ML_BENIGN_WARNINGS)
        try:
            load_ml_driver.LOAD_ML_BENIGN_WARNINGS["something harmless offline"] = "an example entry"
            self.assertEqual(load_ml_driver.check_load_ml_log(log), [])
        finally:
            load_ml_driver.LOAD_ML_BENIGN_WARNINGS.clear()
            load_ml_driver.LOAD_ML_BENIGN_WARNINGS.update(saved)
        for warning, reason in load_ml_driver.LOAD_ML_BENIGN_WARNINGS.items():
            self.assertTrue(reason.strip(), warning)


class TestPeak(unittest.TestCase):
    """Resetting the peak, and the sampler used where that is refused."""

    def test_reset_peak_returns_false_when_clear_refs_is_missing(self):
        """A kernel (or OS) without clear_refs gives False, not an exception."""
        self.assertFalse(load_ml_driver.reset_peak("/nonexistent/clear_refs"))

    def test_peak_sampler_keeps_the_highest_reading(self):
        """The sampler returns the largest RSS it saw."""
        readings = iter([10, 30, 20] + [5] * 1000)
        sampler = load_ml_driver.PeakSampler(lambda: next(readings), interval=0.001)
        sampler.start()
        while sampler.samples < 3:
            pass
        self.assertEqual(sampler.stop(), 30)


class TestArgs(unittest.TestCase):
    """The driver takes container_run.sh's arguments and its own."""

    def test_main_accepts_cycles_profile_tracemalloc_and_timeout(self):
        """--cycles is accepted (container_run.sh always passes it) and ignored; the others are read."""
        args = load_ml_driver.parse_args(["--out", "/out", "--cycles", "0", "--profile", "--timeout", "60"])
        self.assertEqual((args.out, args.profile, args.tracemalloc, args.timeout), ("/out", True, False, 60.0))
        self.assertEqual(load_ml_driver.parse_args(["--out", "/out"]).timeout, 1800)


class TestProfileReport(unittest.TestCase):
    """--profile writes the top functions by cumulative and by own time, and marks the result."""

    def test_write_profile_lists_both_orders_and_marks_the_result(self):
        """profile.txt has both sections, and the result is marked as profiled."""
        import cProfile

        profiler = cProfile.Profile()
        profiler.enable()
        sum(i * i for i in range(1000))
        profiler.disable()
        result = {}
        with tempfile.TemporaryDirectory() as tmp:
            load_ml_driver.write_profile(Path(tmp), profiler, result)
            text = (Path(tmp) / "profile.txt").read_text()
        self.assertIn("by cumulative time", text)
        self.assertIn("by own time", text)
        self.assertIs(result["profiled"], True)


if __name__ == "__main__":
    unittest.main()
