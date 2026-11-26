# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""The memory benchmark's driver, run inside the container by container_run.sh.

    python3 driver.py --out /out --cycles N

Starts Predbat through the same path hass.py's main() takes, from the fixture in the working directory, waits for the
dummy inverter, forecast and Octopus components to publish once, then runs N plan cycles, each forced to recompute (the
wall clock is frozen, so a plan never ages). After each cycle it records the process's RSS; at the end VmHWM. It writes
<out>/result.json with ok, steady_rss_mb, peak_rss_mb and plan_digest, plus diagnostics.
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import traceback
from pathlib import Path

# Warnings the Octopus component logs when its token mint and dispatch poll fail with no network. A failed poll keeps
# what the component has (octopus.py async_update_intelligent_devices), so these are expected in every run.
ALLOWED_WARNINGS = (
    "OctopusAPI: Failed to retrieve auth token",
    "OctopusAPI: Failed to connect",
    "OctopusAPI: Failed to retrieve data from graphql query",
    "OctopusAPI: Could not list intelligent devices, refreshing the",
    "OctopusAPI: Settings fetch failed for intelligent device",
)

# Other warnings a clean benchmark run is known to log, each with the reason it is harmless here
BENIGN_WARNINGS = {
    "Unable to load data from GitHub URL": "the release check has no network in the container; it changes nothing in the plan",
    "Unable to download Predbat version information from GitHub": "the same release check, reported by its caller",
    "credential-like value(s) not using the !secret mechanism": "the generated apps.yaml holds fake keys inline, so there is no secrets.yaml to use",
    "Octopus's catalogue lists charge point": "a note about the real account's charger in the Octopus vehicle catalogue, logged by a real install too",
}

# Components the first cycle must not start without: they publish the inverter, forecast and rates the plan reads
READY_COMPONENTS = ("dummy_inverter", "solar", "octopus")

LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?: (.*)$")
ERROR_RE = re.compile(r"(^|: )Error:")


def check_log(text, allowed_warnings=ALLOWED_WARNINGS, benign_warnings=BENIGN_WARNINGS):
    """Return the problems in a predbat.log, as messages without their timestamps; empty when the run is clean.

    An Error line (also a component's "X: Error:"), a stale kernel binary or Python engine fallback, a forecast URL
    fetch (a request not served from the seeded cache; the "Fetching dual-plane ..." progress line is not one) and any warning not listed as allowed or benign are problems.
    """
    problems = []
    for line in text.splitlines():
        match = LOG_LINE_RE.match(line)
        message = match.group(1) if match else line
        if ERROR_RE.search(message):
            problems.append(message)
        elif "stale binary" in message or "using Python engine" in message:
            problems.append(message)
        elif "SolarAPI: Fetching http" in message:
            problems.append(message)
        elif "Warn:" in message and not any(allowed in message for allowed in tuple(allowed_warnings) + tuple(benign_warnings)):
            problems.append(message)
    return problems


def summarise_rss(samples_kb, hwm_kb, settle):
    """Return {steady_rss_mb, peak_rss_mb}: the median of the samples after the first settle cycles, and VmHWM, in MiB."""
    settled = samples_kb[settle:] or samples_kb
    return {"steady_rss_mb": round(statistics.median(settled) / 1024.0, 1), "peak_rss_mb": round(hwm_kb / 1024.0, 1)}


def canonical_plan(predbat):
    """Return what a cycle decided, in the form the debug-replay goldens compare: the windows, the limits, and validity."""
    from utils import export_limit_sort_key

    return {
        "charge_limit_best": [float(limit) for limit in predbat.charge_limit_best],
        "charge_window_best": predbat.charge_window_best,
        "export_window_best": predbat.export_window_best,
        "export_limits_best": [float(export_limit_sort_key(limit)) for limit in predbat.export_limits_best],
        "plan_valid": bool(predbat.plan_valid),
    }


def plan_digest(plans):
    """Return the sha256 hex digest of every cycle's canonical plan, independent of dict order."""
    return hashlib.sha256(json.dumps(plans, sort_keys=True, default=str).encode()).hexdigest()


def read_status_kb(field, text=None):
    """Return a kB field (VmRSS, VmHWM) from /proc/self/status, or from text; None when it is not there."""
    if text is None:
        text = Path("/proc/self/status").read_text()
    match = re.search(r"^{}:\s+(\d+) kB".format(re.escape(field)), text, re.MULTILINE)
    return int(match.group(1)) if match else None


def wait_ready(predbat, names, timeout):
    """Wait until each of the named components that is enabled has succeeded once; return the names still waiting."""
    waiting = [name for name in names if predbat.components.get_component(name)]
    deadline = time.monotonic() + timeout
    while waiting and time.monotonic() < deadline:
        waiting = [name for name in waiting if predbat.components.last_updated_time(name) is None]
        if waiting:
            time.sleep(0.5)
    return waiting


def run(out, cycles, settle, ready_timeout):
    """Run the benchmark and return the result dict."""
    from multiprocessing import set_start_method

    import predbat

    set_start_method("fork")
    problems = []
    plans = []
    samples = []
    cycle_seconds = []
    began = time.monotonic()
    my_predbat = predbat.PredBat()
    my_predbat.initialize()
    startup_seconds = round(time.monotonic() - began, 1)
    waiting = wait_ready(my_predbat, READY_COMPONENTS, ready_timeout)
    ready_seconds = round(time.monotonic() - began - startup_seconds, 1)
    if waiting:
        problems.append("components never published: {}".format(", ".join(waiting)))
    startup_kb = read_status_kb("VmRSS")
    for cycle in range(cycles):
        began = time.monotonic()
        try:
            if my_predbat.update_pred(scheduled=False) is None:
                problems.append("cycle {} was refused".format(cycle))
        except Exception:
            problems.append("cycle {} raised: {}".format(cycle, traceback.format_exc().strip().splitlines()[-1]))
        cycle_seconds.append(round(time.monotonic() - began, 2))
        plans.append(canonical_plan(my_predbat))
        samples.append(read_status_kb("VmRSS"))
    hwm = read_status_kb("VmHWM")
    log_path = Path("predbat.log")
    problems += check_log(log_path.read_text(errors="replace")) if log_path.exists() else ["no predbat.log"]
    result = {"ok": not problems}
    result.update(summarise_rss(samples, hwm, settle))
    result["plan_digest"] = plan_digest(plans)
    result.update(
        {
            "cycles": cycles,
            "settle": settle,
            "startup_rss_mb": round(startup_kb / 1024.0, 1),
            "rss_samples_mb": [round(kb / 1024.0, 1) for kb in samples],
            "cycle_seconds": cycle_seconds,
            "startup_seconds": startup_seconds,
            "ready_seconds": ready_seconds,
            "problems": problems[:50],
        }
    )
    return result


def main(argv=None):
    """Run the benchmark, write <out>/result.json and exit; 0 when the result was written."""
    parser = argparse.ArgumentParser(description="Memory benchmark driver (runs inside the benchmark container)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cycles", type=int, default=6)
    parser.add_argument("--settle", type=int, default=None, help="cycles left out of the steady figure (default half)")
    parser.add_argument("--ready-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    settle = args.settle if args.settle is not None else args.cycles // 2
    try:
        result = run(Path(args.out), args.cycles, settle, args.ready_timeout)
    except Exception:
        result = {"ok": False, "steady_rss_mb": 0.0, "peak_rss_mb": 0.0, "plan_digest": "", "problems": ["driver raised: " + traceback.format_exc()]}
    (Path(args.out) / "result.json").write_text(json.dumps(result, indent=2))
    sys.stdout.flush()
    # Component threads are still running; leave without waiting for them
    os._exit(0)


if __name__ == "__main__":
    main()
