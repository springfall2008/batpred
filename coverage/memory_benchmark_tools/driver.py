# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""The memory benchmark's driver, run inside the container by container_run.sh.

    python3 driver.py --out /out --cycles N [--tracemalloc]

Starts Predbat through the same path hass.py's main() takes, from the fixture in the working directory, waits for the
dummy inverter, forecast and Octopus components to publish once, then runs N plan cycles, each forced to recompute (the
wall clock is frozen, so a plan never ages). After each cycle it records the process's RSS; at the end VmHWM. It writes
<out>/result.json with ok, steady_rss_mb, peak_rss_mb and plan_digest, plus diagnostics, and <out>/modules.txt, the
loaded modules by top-level package. With --tracemalloc it also
writes <out>/tracemalloc.txt, the largest live allocations after the last cycle, and marks the result as profiled.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrub  # noqa: E402

# Warnings the Octopus component logs when its token mint and dispatch poll fail with no network. A failed poll keeps
# what the component has (octopus.py async_update_intelligent_devices), so these are expected in every run.
# Only the token mint and the Intelligent polls may fail: a failed account, tariff, saving-session or catalogue query
# means a request the seeded cache should have answered went to the network, so it is a problem.
INTELLIGENT_QUERY = r"graphql query get-intelligent-(?:devices|dispatches|settings)\b"
ALLOWED_WARNINGS = (
    r"OctopusAPI: Failed to retrieve auth token",
    r"OctopusAPI: Failed to connect to mint a token",
    r"OctopusAPI: Failed to connect for " + INTELLIGENT_QUERY,
    r"OctopusAPI: Failed to retrieve data from " + INTELLIGENT_QUERY,
    r"OctopusAPI: Could not list intelligent devices, refreshing the",
    r"OctopusAPI: Settings fetch failed for intelligent device",
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

    allowed_warnings are regular expressions; benign_warnings are substrings with the reason each is harmless. A line that
    looks like it holds personal data (an email, a phone number, a postcode other than the fake, real coordinates) is a
    problem whatever else it is, reported by kind only. An Error line (also a component's "X: Error:"), a stale kernel binary or Python engine fallback, a forecast URL
    fetch (a request not served from the seeded cache; the "Fetching dual-plane ..." progress line is not one) and any warning not listed as allowed or benign are problems.
    """
    problems = []
    allowed = [re.compile(pattern) for pattern in allowed_warnings]
    personal = set()
    for line in text.splitlines():
        match = LOG_LINE_RE.match(line)
        message = match.group(1) if match else line
        if "catalogue" not in message:
            # (the catalogue note quotes public vehicle and charger models, some shaped like postcodes)
            personal.update(scrub.personal_data_kinds(message.encode(errors="replace")))
        if ERROR_RE.search(message):
            problems.append(message)
        elif "stale binary" in message or "using Python engine" in message:
            problems.append(message)
        elif "SolarAPI: Fetching http" in message:
            problems.append(message)
        elif "Warn:" in message and not any(pattern.search(message) for pattern in allowed) and not any(benign in message for benign in benign_warnings):
            problems.append(message)
    # By kind only, so the problem list (which may be quoted) never carries the value
    problems += ["possible personal data in the log: {}".format(kind) for kind in sorted(personal)]
    return problems


def summarise_rss(samples_kb, hwm_kb, settle):
    """Return {steady_rss_mb, peak_rss_mb}: the median of the samples after the first settle cycles, and VmHWM, in MiB."""
    settled = samples_kb[settle:] or samples_kb
    return {"steady_rss_mb": round(statistics.median(settled) / 1024.0, 1), "peak_rss_mb": round(hwm_kb / 1024.0, 1)}


def canonical_plan(predbat):
    """Return what a cycle decided: the windows, limits and validity the debug-replay goldens compare, and the predicted SoC and cost."""
    from utils import export_limit_sort_key

    return {
        "charge_limit_best": [float(limit) for limit in predbat.charge_limit_best],
        "charge_window_best": predbat.charge_window_best,
        "export_window_best": predbat.export_window_best,
        "export_limits_best": [float(export_limit_sort_key(limit)) for limit in predbat.export_limits_best],
        "plan_valid": bool(predbat.plan_valid),
        # What the plan predicts, so a saving that changes predictions with the same windows is caught; every half-hour
        "predict_soc_best": _half_hourly(getattr(predbat, "predict_soc_best", {})),
        "predict_metric_best": _half_hourly(getattr(predbat, "predict_metric_best", {})),
    }


def _half_hourly(series):
    """Return a minute-keyed prediction sampled every 30 minutes, with string keys, as the digest's JSON holds it."""
    return {str(minute): value for minute, value in sorted((series or {}).items()) if minute % 30 == 0}


def plan_digest(plans):
    """Return the sha256 hex digest of every cycle's canonical plan, independent of dict order."""
    return hashlib.sha256(json.dumps(plans, sort_keys=True, default=str).encode()).hexdigest()


def read_status_kb(field, text=None):
    """Return a kB field (VmRSS, VmHWM) from /proc/self/status, or from text; None when it is not there."""
    if text is None:
        text = Path("/proc/self/status").read_text()
    match = re.search(r"^{}:\s+(\d+) kB".format(re.escape(field)), text, re.MULTILINE)
    return int(match.group(1)) if match else None


PROFILE_TOP_LINES = 40
PROFILE_TOP_FILES = 25


def finish_profile(out, result):
    """When tracemalloc is tracing (--tracemalloc), write <out>/tracemalloc.txt and mark the result as profiled.

    The report lists the largest live allocations after the last cycle, by line and by file, in MiB. A profiled run's
    RSS includes tracemalloc's own overhead, so the result carries "tracemalloc": true and measure.py refuses it.
    """
    import tracemalloc

    if not tracemalloc.is_tracing():
        return
    snapshot = tracemalloc.take_snapshot().filter_traces((tracemalloc.Filter(False, tracemalloc.__file__),))
    traced_kb, peak_kb = (size // 1024 for size in tracemalloc.get_traced_memory())
    lines = ["Traced now {:.1f} MiB, traced peak {:.1f} MiB".format(traced_kb / 1024.0, peak_kb / 1024.0), ""]
    for title, key, limit in (("line", "lineno", PROFILE_TOP_LINES), ("file", "filename", PROFILE_TOP_FILES)):
        lines.append("Top allocations by {}:".format(title))
        for stat in snapshot.statistics(key)[:limit]:
            frame = stat.traceback[0]
            where = "{}:{}".format(frame.filename, frame.lineno) if key == "lineno" else frame.filename
            lines.append("  {:8.1f} MiB  {:>8} blocks  {}".format(stat.size / 1048576.0, stat.count, where))
        lines.append("")
    (Path(out) / "tracemalloc.txt").write_text("\n".join(lines))
    result["tracemalloc"] = True


def _module_origin(path, stdlib):
    """Return where a module file comes from: third-party (site- or dist-packages), stdlib, or local (Predbat and the benchmark)."""
    if "site-packages" in path or "dist-packages" in path:
        return "third-party"
    if path.startswith(stdlib):
        return "stdlib"
    return "local"


def _compiled_bytes(module):
    """Return the size of a module's compiled file (its .pyc, or the file itself for an extension), or of its source when not compiled."""
    for path in (getattr(module, "__cached__", None), getattr(module, "__file__", None)):
        if path:
            try:
                return os.path.getsize(path)
            except OSError:
                continue
    return 0


def write_module_report(out, result, modules=None, stdlib=None):
    """Write <out>/modules.txt, the loaded modules grouped by top-level package, and put their count in the result.

    Each line is one package: the compiled size of its loaded modules on disk (KiB, a proxy for the code the import
    holds, not its RSS), how many of its modules are loaded, where it comes from and its name, largest first. A
    tracemalloc report puts the cost of importing under importlib, not under the library that was imported, so this is
    how a worker sees which libraries are loaded at all.
    """
    import sysconfig

    modules = sys.modules if modules is None else modules
    stdlib = stdlib or sysconfig.get_paths()["stdlib"]
    packages = {}
    for name, module in list(modules.items()):
        if module is None:
            continue
        path = getattr(module, "__file__", None)
        origin = _module_origin(path, stdlib) if path else "built-in"
        entry = packages.setdefault(name.split(".")[0], {"bytes": 0, "count": 0, "origin": origin})
        if entry["origin"] == "built-in":
            # A namespace package has no file of its own; the first submodule with one says where the package lives
            entry["origin"] = origin
        entry["bytes"] += _compiled_bytes(module)
        entry["count"] += 1
    count = sum(entry["count"] for entry in packages.values())
    lines = ["{} modules loaded in {} top-level packages after the last cycle".format(count, len(packages)), "", "compiled    modules  from         package"]
    for package, entry in sorted(packages.items(), key=lambda item: (-item[1]["bytes"], item[0])):
        lines.append("  {:>6} KiB  {:>5}  {:<11}  {}".format(round(entry["bytes"] / 1024), entry["count"], entry["origin"], package))
    (Path(out) / "modules.txt").write_text("\n".join(lines) + "\n")
    result["modules_loaded"] = count


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
    write_module_report(out, result)
    finish_profile(out, result)
    return result


def parse_args(argv=None):
    """Parse the driver's command line."""
    parser = argparse.ArgumentParser(description="Memory benchmark driver (runs inside the benchmark container)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cycles", type=int, default=6)
    parser.add_argument("--settle", type=int, default=None, help="cycles left out of the steady figure (default half)")
    parser.add_argument("--ready-timeout", type=float, default=180)
    parser.add_argument("--tracemalloc", action="store_true", help="trace allocations from before Predbat is imported and write tracemalloc.txt")
    return parser.parse_args(argv)


def main(argv=None):
    """Run the benchmark, write <out>/result.json and exit; 0 when the result was written."""
    args = parse_args(argv)
    if args.tracemalloc:
        import tracemalloc

        # Started before predbat is imported, so its modules' allocations are traced too. One frame per allocation, which
        # is all the report reads: 25 frames made a run take over ten minutes
        tracemalloc.start(1)
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
