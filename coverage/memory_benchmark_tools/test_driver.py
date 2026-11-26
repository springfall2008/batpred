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

    def test_plan_digest_covers_every_cycle(self):
        """A difference in any one cycle changes the digest."""
        same = [driver.canonical_plan(plan()), driver.canonical_plan(plan())]
        different = [driver.canonical_plan(plan()), driver.canonical_plan(plan(export_limits_best=[50.0]))]
        self.assertNotEqual(driver.plan_digest(same), driver.plan_digest(different))


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
