#!/usr/bin/env python3
"""Unit tests for tools/triage_replay.py.

Run directly with `python3 tools/test_triage_replay.py`. The replay itself (unit_test.py) is never run: every
test that reaches the subprocess patches it, so these cover the wrapper's checks, log joining, command line and
digest only.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import triage_replay  # noqa: E402

YAML_TEXT = "midnight_utc: 2026-11-10 00:00:00+00:00\nminutes_now: 1205\nnow_utc: 2026-11-10 20:05:00+00:00\nnow_utc_real: 2026-11-10 20:05:00.2+00:00\n"


def log_line(stamp, text):
    """A Predbat log line at local time `stamp` ("YYYY-MM-DD HH:MM:SS")."""
    return "{}.123456: {}\n".format(stamp, text)


def run_line(stamp):
    """The line that opens a Predbat run."""
    return log_line(stamp, "--------------- PredBat - update at {}+00:00 with clock skew 0 minutes, minutes now 0".format(stamp))


def version_line(stamp, version):
    """The periodic version report."""
    return log_line(stamp, "Predbat /config/github.py repository springfall2008/batpred version {} currently running, latest version is {}".format(version, version))


SAMPLE_JSON = {
    "meta": {"mode": "exact", "overrides": {}},
    "summary": {
        "runs": 3,
        "adopted": {"compared": 2, "identical": 2},
        "candidate": {"compared": 0, "identical": 0},
        "metrics_differ": 2,
        "clusters": [{"start": "2026-10-26 22:05:00+00:00", "end": "2026-10-26 22:15:00+00:00", "runs": 2, "first_difference": {"line": 2, "live": "pass width 960 best_metric 595.26p", "replay": "pass width 960 best_metric 583.01p"}}],
        "soc_rms_percent": None,
    },
    "runs": [
        {"label": "2026-10-26 22:00", "replanned": True, "soc_percent": 34, "differs": False},
        {
            "label": "2026-10-26 22:05",
            "replanned": True,
            "soc_percent": 33,
            "differs": True,
            "metrics": {"best": {"live": {"metric": 652.3, "keep": 0.0}, "replay": {"metric": 651.51, "keep": 0.0}, "same": False}},
        },
        {
            "label": "2026-10-26 22:15",
            "replanned": True,
            "soc_percent": 32,
            "differs": True,
            "charge": {"live": [{"start": "02:30+1d", "end": "03:00+1d", "rate": 6.6, "percent": 100.0}], "replay": [{"start": "02:30+1d", "end": "03:00+1d", "rate": 6.6, "percent": 30.0}], "same": False},
        },
    ],
}


class TriageReplayTestCase(unittest.TestCase):
    """Gives each test a scratch directory."""

    def setUp(self):
        """Make the scratch directory."""
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        """Remove the scratch directory."""
        self.tmp.cleanup()

    def write(self, name, text):
        """Write a file into the scratch directory and return its path."""
        path = self.dir / name
        path.write_text(text)
        return path


class YamlTimeTests(TriageReplayTestCase):
    """yaml_time() reads now_utc, not now_utc_real or midnight_utc."""

    def test_reads_now_utc(self):
        """The wall-clock part of now_utc, without the offset."""
        self.assertEqual(triage_replay.yaml_time(self.write("d.yaml", YAML_TEXT)), "2026-11-10 20:05:00")

    def test_none_without_now_utc(self):
        """A file that is not a debug yaml gives None."""
        self.assertIsNone(triage_replay.yaml_time(self.write("d.yaml", "foo: 1\n")))


class LogFactsTests(TriageReplayTestCase):
    """log_facts() counts what the replay will have to work with."""

    def test_counts_only_runs_after_the_yaml(self):
        """A run at the yaml's own minute is not after it; lines without a timestamp are skipped."""
        log = self.write("p.log", run_line("2026-11-10 20:00:00") + run_line("2026-11-10 20:05:00") + "Traceback (most recent call last):\n" + run_line("2026-11-10 20:10:00"))
        facts = triage_replay.log_facts(log, after="2026-11-10 20:05:00")
        self.assertEqual(facts["runs_after"], 1)
        self.assertEqual(facts["first"], "2026-11-10 20:00:00")
        self.assertEqual(facts["last"], "2026-11-10 20:10:00")

    def test_version_at_the_yaml_and_after(self):
        """An upgrade before the yaml is the yaml's version, not a change within the replay (GH#6110's log)."""
        log = self.write("p.log", version_line("2026-11-10 10:00:00", "v9.7.8") + version_line("2026-11-10 19:00:00", "v9.7.9") + version_line("2026-11-10 21:00:00", "v9.7.9"))
        facts = triage_replay.log_facts(log, after="2026-11-10 20:05:00")
        self.assertEqual(facts["version_at"], "v9.7.9")
        self.assertEqual(facts["versions_after"], ["v9.7.9"])

    def test_counts_replay_input_lines(self):
        """Replay input lines are what make the replay exact."""
        log = self.write("p.log", log_line("2026-11-10 20:00:00", "Replay input: state soc_kw 1.0") + log_line("2026-11-10 20:00:01", "other"))
        self.assertEqual(triage_replay.log_facts(log)["replay_inputs"], 1)


class CombineLogsTests(TriageReplayTestCase):
    """combine_logs() joins rotated logs oldest first, whatever order they are given in."""

    def test_single_log_is_used_as_is(self):
        """No copy is made of a single log."""
        log = self.write("p.log", run_line("2026-11-10 20:00:00"))
        self.assertEqual(triage_replay.combine_logs([log], self.dir), log)

    def test_joins_oldest_first(self):
        """predbat.log (newest) given first still lands after predbat.log.1."""
        newer = self.write("predbat.log", run_line("2026-11-10 21:00:00"))
        older = self.write("predbat.log.1", run_line("2026-11-10 20:00:00"))
        combined = triage_replay.combine_logs([newer, older], self.dir)
        self.assertEqual(combined.read_text(), older.read_text() + newer.read_text())


class BuildCommandTests(unittest.TestCase):
    """build_command() passes every option through to unit_test.py."""

    def test_options(self):
        """until, simulate and each override reach the command line, with the JSON and chart in out/."""
        out = Path("/scratch/out")
        cmd = triage_replay.build_command(Path("/a/d.yaml"), Path("/a/p.log"), out, until="06:00", simulate=True, overrides=["best_soc_keep=0.5", "set_reserve_min=10"])
        self.assertEqual(cmd[1], "../apps/predbat/unit_test.py")
        self.assertEqual(cmd[cmd.index("--debug_file") + 1], "/a/d.yaml")
        self.assertEqual(cmd[cmd.index("--replay_log") + 1], "/a/p.log")
        self.assertEqual(cmd[cmd.index("--replay_json") + 1], str(out / "replay.json"))
        self.assertEqual(cmd[cmd.index("--replay_chart") + 1], str(out / "replay.png"))
        self.assertEqual(cmd[cmd.index("--replay_until") + 1], "06:00")
        self.assertIn("--replay_simulate", cmd)
        self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--override"], ["best_soc_keep=0.5", "set_reserve_min=10"])

    def test_defaults(self):
        """Without options there is no until, simulate or override."""
        cmd = triage_replay.build_command(Path("/a/d.yaml"), Path("/a/p.log"), Path("/o"))
        for flag in ("--replay_until", "--replay_simulate", "--override"):
            self.assertNotIn(flag, cmd)


class DigestTests(unittest.TestCase):
    """digest() leads with what differs, not with the export-only headline."""

    def setUp(self):
        """Digest the sample once."""
        self.text = "\n".join(triage_replay.digest(SAMPLE_JSON))

    def test_export_headline_is_qualified(self):
        """The identical count is flagged as export windows only."""
        self.assertIn("adopted export windows identical: 2 of 2 re-plans (export windows only", self.text)

    def test_counts_charge_and_metric_differences(self):
        """Charge-plan differences are counted from the runs, as the summary has no count for them."""
        self.assertIn("runs whose charge plan differs: 1", self.text)
        self.assertIn("runs whose predicted cost/SoC differs: 2", self.text)

    def test_cluster_first_difference(self):
        """Each cluster's first differing plan-trace line is shown live against replay."""
        self.assertIn("first difference, plan-trace line 2", self.text)
        self.assertIn("replay: pass width 960 best_metric 583.01p", self.text)

    def test_plan_changes_listed_first(self):
        """The run whose charge plan changed comes before the earlier run where only the metric moved."""
        self.assertLess(self.text.index("run 2026-10-26 22:15"), self.text.index("run 2026-10-26 22:05"))
        self.assertIn("charge live:   02:30+1d-03:00+1d 100.0%", self.text)
        self.assertIn("charge replay: 02:30+1d-03:00+1d 30.0%", self.text)

    def test_metric_change_shows_only_differing_fields(self):
        """keep is equal, so only metric is shown, with its signed change."""
        self.assertIn("best: metric 652.3 -> 651.51 (-0.79)", self.text)
        self.assertNotIn("keep", self.text)

    def test_limit(self):
        """Runs past the limit are counted, not listed."""
        text = "\n".join(triage_replay.digest(SAMPLE_JSON, limit=1))
        self.assertIn("... 1 more differing runs in the JSON", text)


class MainTests(TriageReplayTestCase):
    """main() stops before the replay when there is nothing to replay, and otherwise runs it from coverage/."""

    def test_nothing_after_the_yaml_exits_3_without_running(self):
        """The GH#6150 shape: the yaml was taken at the end of the log."""
        yaml_path = self.write("d.yaml", YAML_TEXT)
        log = self.write("p.log", run_line("2026-11-10 19:55:00") + run_line("2026-11-10 20:00:00") + log_line("2026-11-10 20:06:00", "Update event install"))
        with patch.object(triage_replay.subprocess, "run") as run, patch("builtins.print"):
            status = triage_replay.main(["--yaml", str(yaml_path), "--log", str(log), "--out", str(self.dir / "out")])
        self.assertEqual(status, 3)
        run.assert_not_called()

    def test_missing_file_exits_2(self):
        """A path that does not exist is reported, not passed on."""
        status = triage_replay.main(["--yaml", str(self.dir / "nope.yaml"), "--log", str(self.dir / "nope.log"), "--out", str(self.dir / "out")])
        self.assertEqual(status, 2)

    def test_runs_from_coverage_and_digests(self):
        """The replay runs with coverage/ as its working directory and the digest is printed."""
        yaml_path = self.write("d.yaml", YAML_TEXT)
        log = self.write("p.log", run_line("2026-11-10 20:10:00"))
        out = self.dir / "out"

        def fake_run(cmd, cwd, stdout, stderr, check):
            """Stand in for the replay: write the JSON it would have written."""
            Path(cmd[cmd.index("--replay_json") + 1]).write_text(json.dumps(SAMPLE_JSON))
            self.assertEqual(Path(cwd), triage_replay.COVERAGE_DIR)
            return unittest.mock.Mock(returncode=0)

        with patch.object(triage_replay.subprocess, "run", side_effect=fake_run), patch.object(triage_replay, "COVERAGE_DIR", self.dir), patch("builtins.print") as printed:
            status = triage_replay.main(["--yaml", str(yaml_path), "--log", str(log), "--out", str(out)])
        self.assertEqual(status, 0)
        output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Predbat runs in the log after the yaml: 1", output)
        self.assertIn("runs whose charge plan differs: 1", output)

    def test_failed_replay_returns_its_status(self):
        """A failing replay's status comes back and no stale JSON is digested."""
        yaml_path = self.write("d.yaml", YAML_TEXT)
        log = self.write("p.log", run_line("2026-11-10 20:10:00"))
        with patch.object(triage_replay.subprocess, "run", return_value=unittest.mock.Mock(returncode=1)), patch.object(triage_replay, "COVERAGE_DIR", self.dir), patch("builtins.print"):
            status = triage_replay.main(["--yaml", str(yaml_path), "--log", str(log), "--out", str(self.dir / "out")])
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
