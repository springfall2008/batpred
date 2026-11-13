#!/usr/bin/env python3
"""Replay a reporter's predbat.log forwards from their predbat_debug.yaml, for the triage bot.

usage: python3 tools/triage_replay.py --yaml <predbat_debug.yaml> --log <predbat.log> [--log <older.log> ...]
                                      --out <dir> [--until HH:MM] [--simulate] [--override name=value ...]

A wrapper round `coverage/run_all --debug_file ... --replay_log ...` (the forward replay, see "Replaying a log
forwards" in docs/developing.md). It exists for the same reason as tools/triage_test.sh: run_all has to run with
coverage/ as its working directory, and the `cd coverage && ./run_all ... > log` form is refused under the bot's
non-interactive permission mode. On top of that it:

- checks before running that the log has Predbat runs after the moment the yaml was taken. A reporter usually
  takes the yaml when they file the issue, at the end of the log, which leaves nothing to replay (GH#6150);
- joins several logs (rotated predbat.log.1, .2 ...) oldest first, ordered by their first timestamp;
- writes the replay's output, JSON, chart and its own predbat.log into --out;
- prints a digest of the JSON, because its headline "identical" counts compare export windows only and say
  nothing on a house that plans no exports - the charge plans and predicted costs are where differences show.

Exit status: 0 the replay ran, 2 bad arguments or a missing file, 3 nothing to replay, otherwise run_all's status.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_DIR = REPO_ROOT / "coverage"

# Every Predbat log line starts with a local wall-clock timestamp; the yaml's now_utc is local time too
# (despite the name), so the two compare as plain "YYYY-MM-DD HH:MM:SS" strings.
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
YAML_NOW_RE = re.compile(r"^now_utc: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RUN_MARK = "PredBat - update at"
REPLAY_INPUT_MARK = "Replay input:"
VERSION_RE = re.compile(r"version (v[\d.]+) currently running")

DIGEST_RUN_LIMIT = 8
# The plans a differing run record can carry, live against replay: export windows adopted, export windows the
# re-plan proposed before the adopt decision, and charge windows
PLAN_NAMES = ("adopted", "candidate", "charge")


def yaml_time(path):
    """Return the yaml's now_utc as "YYYY-MM-DD HH:MM:SS", or None if it has none."""
    with open(path, errors="replace") as handle:
        for line in handle:
            match = YAML_NOW_RE.match(line)
            if match:
                return match.group(1)
    return None


def log_facts(path, after=None):
    """Scan a log once: its first and last timestamps, how many Predbat runs start after `after`, how many
    "Replay input:" lines it has, the Predbat version running when `after` was reached, and every version
    reported after it, in order seen."""
    facts = {"first": None, "last": None, "runs_after": 0, "replay_inputs": 0, "version_at": None, "versions_after": []}
    with open(path, errors="replace") as handle:
        for line in handle:
            match = TIMESTAMP_RE.match(line)
            if not match:
                continue
            stamp = match.group(1)
            if facts["first"] is None:
                facts["first"] = stamp
            facts["last"] = stamp
            later = after is None or stamp > after
            if RUN_MARK in line and later:
                facts["runs_after"] += 1
            if REPLAY_INPUT_MARK in line:
                facts["replay_inputs"] += 1
            version = VERSION_RE.search(line)
            if version:
                if not later:
                    facts["version_at"] = version.group(1)
                elif version.group(1) not in facts["versions_after"]:
                    facts["versions_after"].append(version.group(1))
    return facts


def combine_logs(paths, out_dir):
    """Return a single log to replay: the only one given, or all of them joined oldest first
    (ordered by first timestamp) into <out_dir>/combined.log."""
    if len(paths) == 1:
        return paths[0]

    def first_stamp(path):
        """The log's first timestamp, with an undated log sorted last."""
        return log_facts(path)["first"] or "9999"

    combined = out_dir / "combined.log"
    with open(combined, "wb") as out:
        for path in sorted(paths, key=first_stamp):
            with open(path, "rb") as handle:
                shutil.copyfileobj(handle, out)
    return combined


def replay_python():
    """The Python to run the replay with: coverage/venv's (made by coverage/setup.csh) when it exists.

    unit_test.py is run directly rather than through ./run_all, which has no #! line (a shell runs it as a
    script, subprocess cannot) and would re-run pip install on every replay."""
    venv_python = COVERAGE_DIR / "venv" / "bin" / "python3"
    return str(venv_python) if venv_python.exists() else sys.executable


def build_command(yaml_path, log_path, out_dir, until=None, simulate=False, overrides=None):
    """The command line for the replay, run from coverage/, with every path absolute."""
    cmd = [replay_python(), "../apps/predbat/unit_test.py", "--debug_file", str(yaml_path), "--replay_log", str(log_path), "--replay_json", str(out_dir / "replay.json"), "--replay_chart", str(out_dir / "replay.png")]
    if until:
        cmd += ["--replay_until", until]
    if simulate:
        cmd.append("--replay_simulate")
    for override in overrides or []:
        cmd += ["--override", override]
    return cmd


def format_windows(windows):
    """A window list as compact text, e.g. "23:30-00:00+1d 22%, 02:30+1d-03:00+1d 100%"."""
    if not windows:
        return "none"
    parts = []
    for window in windows:
        text = "{}-{}".format(window.get("start"), window.get("end"))
        if window.get("percent") is not None:
            text += " {}%".format(window["percent"])
        parts.append(text)
    return ", ".join(parts)


def format_metric_change(live, replay):
    """The prediction-summary fields that differ, as "metric 652.3 -> 651.51 (-0.79), keep -0.14 -> 0.0"."""
    parts = []
    for field in sorted(set(live) | set(replay)):
        before, after = live.get(field), replay.get(field)
        if before == after:
            continue
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            parts.append("{} {} -> {} ({:+.2f})".format(field, before, after, after - before))
        else:
            parts.append("{} {} -> {}".format(field, before, after))
    return ", ".join(parts) or "same values"


def digest(data, limit=DIGEST_RUN_LIMIT):
    """Summarise a --replay_json document as lines of text, leading with what actually differs."""
    meta, summary, runs = data.get("meta") or {}, data.get("summary") or {}, data.get("runs") or []
    differing = [run for run in runs if run.get("differs")]
    lines = [
        "mode: {}{}".format(meta.get("mode", "?"), " overrides {}".format(meta["overrides"]) if meta.get("overrides") else ""),
        "runs replayed: {}, of which differ from the live log: {}".format(summary.get("runs", len(runs)), len(differing)),
    ]
    for name in ("adopted", "candidate"):
        counts = summary.get(name) or {}
        lines.append("{} export windows identical: {} of {} re-plans (export windows only - says nothing if no exports were planned)".format(name, counts.get("identical", 0), counts.get("compared", 0)))
    lines.append("runs whose charge plan differs: {}".format(sum(1 for run in differing if "charge" in run)))
    lines.append("runs whose predicted cost/SoC differs: {}".format(summary.get("metrics_differ", 0)))
    if summary.get("soc_rms_percent") is not None:
        lines.append("simulated SoC vs logged SoC: {}% RMS".format(summary["soc_rms_percent"]))
    for cluster in summary.get("clusters") or []:
        lines.append("cluster {} .. {} ({} runs)".format(cluster.get("start"), cluster.get("end"), cluster.get("runs")))
        first = cluster.get("first_difference")
        if first:
            lines.append("  first difference, plan-trace line {}:".format(first.get("line")))
            lines.append("    live:   {}".format(first.get("live")))
            lines.append("    replay: {}".format(first.get("replay")))
    # Runs whose plan changed first: a differing number alone (a carbon total, a keep figure) rarely matters
    plan_changed = [run for run in differing if any(name in run for name in PLAN_NAMES)]
    shown = (plan_changed + [run for run in differing if run not in plan_changed])[:limit]
    for run in shown:
        lines.append("run {} (SoC {}%):".format(run.get("label"), run.get("soc_percent")))
        for name in PLAN_NAMES:
            if name in run:
                lines.append("  {} live:   {}".format(name, format_windows(run[name].get("live"))))
                lines.append("  {} replay: {}".format(name, format_windows(run[name].get("replay"))))
        for name, pair in sorted((run.get("metrics") or {}).items()):
            lines.append("  {}: {}".format(name, format_metric_change(pair.get("live") or {}, pair.get("replay") or {})))
        if not any(name in run for name in PLAN_NAMES) and not run.get("metrics") and run.get("first_difference"):
            first = run["first_difference"]
            lines.append("  plan trace only, line {}: live {!r} / replay {!r}".format(first.get("line"), first.get("live"), first.get("replay")))
    if len(differing) > limit:
        lines.append("... {} more differing runs in the JSON".format(len(differing) - limit))
    return lines


def parse_args(argv):
    """Parse the wrapper's arguments."""
    parser = argparse.ArgumentParser(description="Replay a Predbat log forwards from a debug yaml (triage bot wrapper).")
    parser.add_argument("--yaml", required=True, help="the predbat_debug.yaml to start from")
    parser.add_argument("--log", required=True, action="append", help="the predbat.log to replay; repeat for rotated logs, in any order")
    parser.add_argument("--out", required=True, help="directory for the output, JSON, chart and the replay's own predbat.log")
    parser.add_argument("--until", help="stop at the first HH:MM after the yaml")
    parser.add_argument("--simulate", action="store_true", help="step the battery under the replayed plan instead of taking SoC from the log")
    parser.add_argument("--override", action="append", help="name=value setting change after the yaml is restored (repeatable)")
    return parser.parse_args(argv)


def main(argv=None):
    """Check the inputs, run the replay from coverage/, and print a digest of its JSON."""
    args = parse_args(argv)
    yaml_path = Path(args.yaml).resolve()
    log_paths = [Path(path).resolve() for path in args.log]
    for path in [yaml_path] + log_paths:
        if not path.is_file():
            print("missing file: {}".format(path), file=sys.stderr)
            return 2
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    taken = yaml_time(yaml_path)
    if taken is None:
        print("{} has no now_utc line - is it a predbat_debug.yaml?".format(yaml_path), file=sys.stderr)
        return 2
    log_path = combine_logs(log_paths, out_dir)
    facts = log_facts(log_path, after=taken)
    print("yaml taken at {}; log covers {} .. {}".format(taken, facts["first"], facts["last"]))
    print(
        "Predbat runs in the log after the yaml: {}; 'Replay input:' lines: {}; version at the yaml: {}; versions reported after it: {}".format(
            facts["runs_after"], facts["replay_inputs"], facts["version_at"] or "unknown", ", ".join(facts["versions_after"]) or "none"
        )
    )
    if facts["runs_after"] == 0:
        print(
            "Nothing to replay: the yaml was taken at or after the log's last Predbat run. A forward replay needs a yaml "
            "from the start of the period in question - ask for an earlier snapshot (Plan -> History, Debug column), "
            "or use a plain debug-file replay of this yaml instead."
        )
        return 3
    if facts["first"] and facts["first"] > taken:
        print("Note: the log starts after the yaml was taken, so the replay's day counters start from the yaml, not the log.")
    if facts["replay_inputs"] == 0:
        print("Note: no 'Replay input:' lines (a Predbat from before they were added), so the forecasts come from the yaml - expect approximate plans, not a match.")
    if any(version != facts["version_at"] for version in facts["versions_after"]):
        print("Note: the version changes within the log; plans after the change are counted separately.")

    cmd = build_command(yaml_path, log_path, out_dir, until=args.until, simulate=args.simulate, overrides=args.override)
    output = out_dir / "replay_out.log"
    replay_log = COVERAGE_DIR / "predbat.log"
    # The replay writes its own plan trace here; clear the last run's so a failed replay cannot pass it off as its own
    replay_log.unlink(missing_ok=True)
    json_path = out_dir / "replay.json"
    json_path.unlink(missing_ok=True)
    with open(output, "w") as handle:
        status = subprocess.run(cmd, cwd=COVERAGE_DIR, stdout=handle, stderr=subprocess.STDOUT, check=False).returncode
    if replay_log.is_file():
        shutil.copyfile(replay_log, out_dir / "replay_predbat.log")
    print("replay exit={} output={}".format(status, output))

    if status != 0 or not json_path.is_file():
        print("---- last 30 lines of the output ----")
        with open(output, errors="replace") as handle:
            print("".join(handle.readlines()[-30:]), end="")
        return status or 1
    with open(json_path) as handle:
        data = json.load(handle)
    print("---- digest of {} ----".format(json_path))
    print("\n".join(digest(data)))
    print("full JSON: {}; chart: {}; replay's own log: {}".format(json_path, out_dir / "replay.png", out_dir / "replay_predbat.log"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
