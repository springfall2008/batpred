# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the memory benchmark driver's result rules: the log check, the RSS summary and the plan digest."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "apps" / "predbat"))

import driver  # noqa: E402

CLEAN = "2026-11-26 09:17:18.000001: Predbat: Startup\n2026-11-26 09:17:19.000001: Info: Plan calculated\n"


def plan(**overrides):
    """A stand-in PredBat holding a plan, with run-time attributes that are not part of the plan."""
    fields = {
        "charge_limit_best": [10.0, 4.0],
        "charge_window_best": [{"start": 60, "end": 120, "average": 10.5}],
        "export_window_best": [{"start": 900, "end": 930, "average": 25.0}],
        "export_limits_best": [99.0],
        "plan_valid": True,
        "predict_soc_best": {0: 5.0, 5: 5.1, 30: 6.0},
        "predict_metric_best": {0: 0.0, 5: 0.1, 30: 1.25},
        "plan_last_updated": "2026-11-26 09:17:18",
        "plan_last_updated_minutes": 557,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestCheckLog(unittest.TestCase):
    """check_log lists what makes a run not ok."""

    def test_check_log_passes_a_clean_log(self):
        """A log with no errors or warnings has no problems."""
        self.assertEqual(driver.check_log(CLEAN), [])

    def test_check_log_flags_errors_stale_binary_and_solar_fetch(self):
        """An Error line, a component's Error, a stale kernel binary and a forecast fetch are each a problem."""
        for line in (
            "Error: Failed to fetch inverter data",
            "Storage: Error: Failed to write metadata",
            "Warn: Prediction kernel stale binary /work/x.so (abi 1 revision 2) - using Python engine",
            "SolarAPI: Fetching https://api.forecast.solar/benchmark/estimate/1/2/3/4/5?time=utc",
        ):
            problems = driver.check_log(CLEAN + "2026-11-26 09:17:20.000001: {}\n".format(line))
            self.assertEqual(len(problems), 1, line)

    def test_check_log_does_not_take_the_forecast_progress_line_for_a_fetch(self):
        """The component's "Fetching dual-plane ..." progress line is logged before the cache answers; only a URL fetch counts."""
        line = "2026-11-26 09:17:20.000001: SolarAPI: Fetching dual-plane Forecast Solar for lat 51.501 lon -0.1419\n"
        self.assertEqual(driver.check_log(CLEAN + line), [])

    def test_check_log_allows_only_the_named_octopus_warnings(self):
        """The failed Octopus token and poll warnings are allowed; any other warning is a problem."""
        allowed = "2026-11-26 09:17:20.000001: Warn: OctopusAPI: Failed to retrieve auth token\n"
        other = "2026-11-26 09:17:20.000001: Warn: Something unexpected happened\n"
        self.assertEqual(driver.check_log(CLEAN + allowed, allowed_warnings=("OctopusAPI: Failed to retrieve auth token",)), [])
        self.assertEqual(len(driver.check_log(CLEAN + other, allowed_warnings=("OctopusAPI: Failed to retrieve auth token",))), 1)

    def test_offline_octopus_and_version_check_warnings_are_not_problems(self):
        """The warnings a run with no network always logs (Octopus polls, the GitHub version check) are expected."""
        lines = [
            "Warn: OctopusAPI: Failed to retrieve data from graphql query get-intelligent-devices - token refresh failed",
            "Warn: OctopusAPI: Could not list intelligent devices, refreshing the 1 known device(s) instead",
            "Warn: OctopusAPI: Settings fetch failed for intelligent device 0000, reusing the last known settings",
            "Warn: OctopusAPI: Failed to connect to mint a token: Cannot connect to host",
            "Warn: Unable to load data from GitHub URL: https://api.github.com/repos/springfall2008/batpred/releases",
            "Warn: Unable to download Predbat version information from GitHub, return code: []",
        ]
        text = CLEAN + "".join("2026-11-26 09:17:20.000001: {}\n".format(line) for line in lines)
        self.assertEqual(driver.check_log(text), [])

    def test_check_log_flags_an_unlisted_octopus_query(self):
        """Only the Intelligent polls may fail offline; a failed account query means a request was not served from the cache."""
        for line in ("Warn: OctopusAPI: Failed to connect for graphql query get-account: Cannot connect", "Warn: OctopusAPI: Failed to retrieve data from graphql query get-saving-sessions - token refresh failed"):
            self.assertEqual(len(driver.check_log(CLEAN + "2026-11-26 09:17:20.000001: {}\n".format(line))), 1, line)

    def test_check_log_flags_personal_data_by_kind(self):
        """A log line that looks like it holds an email is a problem, reported without the value."""
        problems = driver.check_log(CLEAN + "2026-11-26 09:17:20.000001: Info: account owner jo@example.org\n")
        self.assertEqual(problems, ["possible personal data in the log: email"])

    def test_check_log_does_not_take_a_catalogue_model_name_for_a_postcode(self):
        """The benign catalogue note quotes vehicle and charger models, which can look like postcodes."""
        line = "2026-11-26 09:17:20.000001: Warn: OctopusAPI: Octopus's catalogue lists charge point Tesla 'Model S P90DL' at more than one power\n"
        self.assertEqual(driver.check_log(CLEAN + line), [])

    def test_benign_warnings_each_carry_a_reason(self):
        """Every benign warning says why it is harmless in the benchmark."""
        for substring, reason in driver.BENIGN_WARNINGS.items():
            self.assertTrue(substring and len(reason) > 20, substring)

    def test_check_log_reports_lines_without_their_timestamps(self):
        """A problem is reported as the message, so two runs' problems compare equal."""
        problems = driver.check_log(CLEAN + "2026-11-26 09:17:20.000001: Error: boom\n")
        self.assertEqual(problems, ["Error: boom"])


class TestSummariseRss(unittest.TestCase):
    """summarise_rss turns kB samples into the steady and peak figures."""

    def test_summarise_rss_uses_median_after_settle_and_hwm_for_peak(self):
        """Steady is the median after the settle cycles; peak is VmHWM; both in MiB to 0.1."""
        result = driver.summarise_rss([102400, 122880, 121856, 123904, 122880], 133120, settle=2)
        self.assertEqual(result, {"steady_rss_mb": 120.0, "peak_rss_mb": 130.0})

    def test_summarise_rss_with_nothing_after_settle_uses_every_sample(self):
        """With no samples past the settle count, every sample counts rather than none."""
        self.assertEqual(driver.summarise_rss([102400], 102400, settle=3)["steady_rss_mb"], 100.0)


class TestPlanDigest(unittest.TestCase):
    """plan_digest hashes the canonical plan of every cycle."""

    def test_plan_digest_is_stable_and_order_insensitive_for_dicts_and_changes_with_a_plan(self):
        """The same plans give the same digest whatever the dict order; a different window gives another."""
        first = driver.plan_digest([driver.canonical_plan(plan())])
        reordered = driver.plan_digest([dict(reversed(list(driver.canonical_plan(plan()).items())))])
        changed = driver.plan_digest([driver.canonical_plan(plan(charge_window_best=[{"start": 60, "end": 150, "average": 10.5}]))])
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)
        self.assertRegex(first, "^[0-9a-f]{64}$")

    def test_plan_digest_ignores_run_timestamps(self):
        """When the plan was made is not part of the plan."""
        self.assertEqual(driver.plan_digest([driver.canonical_plan(plan())]), driver.plan_digest([driver.canonical_plan(plan(plan_last_updated="2027-01-01 00:00:00", plan_last_updated_minutes=1))]))

    def test_plan_digest_includes_the_predicted_soc_and_cost(self):
        """A change to the predicted cost or SoC at a half-hour changes the digest even with the same windows."""
        base = driver.plan_digest([driver.canonical_plan(plan())])
        self.assertNotEqual(base, driver.plan_digest([driver.canonical_plan(plan(predict_metric_best={0: 0.0, 5: 0.1, 30: 1.26}))]))
        self.assertNotEqual(base, driver.plan_digest([driver.canonical_plan(plan(predict_soc_best={0: 5.0, 5: 5.1, 30: 6.1}))]))

    def test_plan_digest_covers_every_cycle(self):
        """A difference in any one cycle changes the digest."""
        same = [driver.canonical_plan(plan()), driver.canonical_plan(plan())]
        different = [driver.canonical_plan(plan()), driver.canonical_plan(plan(export_limits_best=[50.0]))]
        self.assertNotEqual(driver.plan_digest(same), driver.plan_digest(different))


class TestProfile(unittest.TestCase):
    """finish_profile writes the --tracemalloc report and marks the result as profiled."""

    def test_finish_profile_writes_the_top_allocations_and_marks_the_result(self):
        """While tracemalloc traces, the report lists allocations by line and by file, and the result says tracemalloc."""
        import tempfile
        import tracemalloc

        tracemalloc.start(5)
        try:
            held = [bytearray(1024) for _ in range(512)]  # noqa: F841 - kept alive so the snapshot sees it
            result = {"ok": True}
            with tempfile.TemporaryDirectory() as out:
                driver.finish_profile(Path(out), result)
                report = (Path(out) / "tracemalloc.txt").read_text()
        finally:
            tracemalloc.stop()
        self.assertTrue(result["tracemalloc"])
        self.assertIn("Top allocations by line", report)
        self.assertIn("Top allocations by file", report)
        self.assertIn("test_driver.py", report)
        self.assertRegex(report, r"\d+\.\d MiB")

    def test_finish_profile_does_nothing_when_not_tracing(self):
        """A normal run writes no report and is not marked as profiled."""
        import tempfile

        result = {"ok": True}
        with tempfile.TemporaryDirectory() as out:
            driver.finish_profile(Path(out), result)
            self.assertFalse((Path(out) / "tracemalloc.txt").exists())
        self.assertNotIn("tracemalloc", result)

    def test_main_accepts_the_tracemalloc_flag(self):
        """--tracemalloc is a driver option, passed in by container_run.sh."""
        self.assertTrue(driver.parse_args(["--out", "/out", "--tracemalloc"]).tracemalloc)
        self.assertFalse(driver.parse_args(["--out", "/out"]).tracemalloc)


class TestModuleReport(unittest.TestCase):
    """write_module_report lists the loaded modules by top-level package, so workers can see which libraries are loaded."""

    def make_modules(self, root):
        """Fake sys.modules: a two-module third-party package, a stdlib module, a Predbat module, a built-in and a None entry."""
        site = Path(root) / "lib" / "site-packages"
        stdlib = Path(root) / "lib" / "python3"
        tree = Path(root) / "apps" / "predbat"
        for directory in (site / "heavy" / "__pycache__", stdlib / "__pycache__", tree):
            directory.mkdir(parents=True)

        def module(name, source, cached=None, cached_bytes=0):
            """A module whose compiled file holds cached_bytes."""
            source.write_text("x = 1\n")
            if cached:
                cached.write_bytes(b"\0" * cached_bytes)
            return SimpleNamespace(__name__=name, __file__=str(source), __cached__=str(cached) if cached else None)

        return {
            "heavy": module("heavy", site / "heavy" / "__init__.py", site / "heavy" / "__pycache__" / "a.pyc", 300 * 1024),
            "heavy.sub": module("heavy.sub", site / "heavy" / "sub.py", site / "heavy" / "__pycache__" / "b.pyc", 200 * 1024),
            "json": module("json", stdlib / "json.py", stdlib / "__pycache__" / "json.pyc", 100 * 1024),
            # No compiled file (a read-only tree): the source's size stands in
            "fetch": module("fetch", tree / "fetch.py"),
            "sys": SimpleNamespace(__name__="sys"),
            "gone": None,
            # A namespace package has no __file__ of its own (ruamel.yaml); its submodules say where it comes from
            "nested": SimpleNamespace(__name__="nested"),
            "nested.inner": module("nested.inner", site / "nested_inner.py", site / "heavy" / "__pycache__" / "c.pyc", 50 * 1024),
        }, str(stdlib)

    def test_groups_by_package_largest_first_and_counts_the_modules(self):
        """Each top-level package is one line with its module count, compiled KiB and where it comes from; the result gets the count."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            modules, stdlib = self.make_modules(root)
            result = {"ok": True}
            driver.write_module_report(Path(root), result, modules=modules, stdlib=stdlib)
            report = (Path(root) / "modules.txt").read_text()
        self.assertEqual(result["modules_loaded"], 7)
        self.assertIn("7 modules loaded", report)
        rows = [line.split() for line in report.splitlines() if line.startswith("  ")]
        self.assertEqual([row[-1] for row in rows], ["heavy", "json", "nested", "fetch", "sys"])
        self.assertEqual(rows[0][:4], ["500", "KiB", "2", "third-party"])
        self.assertEqual(rows[1][:4], ["100", "KiB", "1", "stdlib"])
        self.assertEqual(rows[2][:4], ["50", "KiB", "2", "third-party"])
        self.assertEqual(rows[3][2:4], ["1", "local"])
        self.assertEqual(rows[4][:4], ["0", "KiB", "1", "built-in"])


class TestStatus(unittest.TestCase):
    """read_status_kb reads a field from a /proc status file."""

    def test_read_status_kb(self):
        """VmRSS and VmHWM are read in kB; a missing field is None."""
        text = "Name:\tpython3\nVmHWM:\t  163840 kB\nVmRSS:\t  143360 kB\n"
        self.assertEqual(driver.read_status_kb("VmRSS", text), 143360)
        self.assertEqual(driver.read_status_kb("VmHWM", text), 163840)
        self.assertIsNone(driver.read_status_kb("VmSwap", text))


if __name__ == "__main__":
    unittest.main()
