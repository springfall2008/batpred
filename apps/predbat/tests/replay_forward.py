# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Replay a Predbat log forwards from a debug yaml.

A debug yaml is a single snapshot. A bug report usually also carries the log from the following hours, which
records what the battery, PV and load actually did and what each re-plan produced. This module restores the
yaml, then steps through the log one Predbat run at a time: it sets the clock, SoC, history and the inverter's
export window from the log, re-plans on the runs where the real Predbat did, and compares the export windows
it gets with the ones the log shows.

The comparison is the point. Where the replay reproduces the logged windows it can be trusted for what-if
changes; where it does not, the first run that diverges says what the log carries that the yaml did not.

Inputs taken from the log rather than recomputed: SoC, the cumulative load/PV/import/export counters, the
in-day load adjustment, the PV cloud factor, and the inverter's programmed export window. The PV forecast is
the one in the yaml - the log records only its total.
"""
import array
import re
from datetime import timedelta

from utils import MinuteArray
from prediction import Prediction
from tests.test_single_debug import restore_debug_state, rebuild_load_pv_models, rescan_rate_windows

RUN_RE = re.compile(r"PredBat - update at (\S+ \S+) with clock skew .*minutes now (\d+)")
SOC_RE = re.compile(r"Inverter 0 SoC: ([\d.]+)kWh (\d+)%.*current battery power (-?\d+)W")
TODAY_RE = re.compile(r"Current data so far today: load ([\d.]+)kWh, import ([\d.]+)kWh, export ([\d.]+)kWh, PV ([\d.]+)kWh")
INDAY_RE = re.compile(r"in-day adjustment ([\d.]+)%")
CLOUD_RE = re.compile(r"PV cloud factor ([\d.]+)")
FILTERED_RE = re.compile(r"Export windows filtered (\[.*\])")
FORCE_RE = re.compile(r"Inverter 0 Adjust force export to (True|False), change times from \S+ - \S+ to (\d+):(\d+):\d+ - (\d+):(\d+):\d+")
WINDOW_RE = re.compile(r"(\d\d-\d\d) (\d\d):(\d\d):\d\d - (\d\d-\d\d) (\d\d):(\d\d):\d\d @ ([\d.]+)\S+ ([\d.]+)%")
# The four day counters in the order the log prints them, and the history arrays that mirror them
COUNTER_ARRAYS = ("load_minutes", "import_today", "export_today", "pv_today")


def parse_windows(text):
    """Parse a window_as_text string into (start HH:MM, end HH:MM, rate, percent) tuples."""
    return [("{}:{}".format(m[1], m[2]), "{}:{}".format(m[4], m[5]), float(m[6]), float(m[7])) for m in WINDOW_RE.findall(text)]


def parse_log(path):
    """Split a Predbat log into runs, one per "PredBat - update" line, keeping what the replay needs.

    Several runs can share a minutes_now (a scheduled run plus one triggered by a sensor change), so a run
    is keyed by its position in the log, not by its time. A run that never logged a SoC is dropped.
    """
    runs = []
    run = None
    with open(path, "r", errors="replace") as handle:
        for line in handle:
            marker = RUN_RE.search(line)
            if marker:
                run = {"time": marker.group(1), "minutes_now": int(marker.group(2)), "filtered": None, "force": None}
                runs.append(run)
                continue
            if run is None:
                continue
            for regex, store in ((SOC_RE, "soc"), (TODAY_RE, "today"), (INDAY_RE, "inday"), (CLOUD_RE, "cloud"), (FILTERED_RE, "filtered"), (FORCE_RE, "force")):
                found = regex.search(line)
                if found:
                    run[store] = found.groups() if store in ("soc", "today", "force") else found.group(1)
    return [run for run in runs if run.get("soc")]


def shift_counter(history, minutes, delta):
    """Advance a backwards cumulative history by minutes, adding delta kWh over the gap.

    The history is indexed by minutes ago, with index 0 the latest counter and larger indexes older (smaller)
    values, so advancing time moves every existing entry to a higher index and the gap fills with a ramp from
    the old latest value up to the new one. The growth is spread evenly across the gap, which is all the log's
    once-per-run counters can tell us.
    """
    if minutes <= 0:
        return history
    old_latest = history.get(0, 0.0)
    shifted = array.array("d", [0.0]) * (len(history) + minutes)
    for key in range(minutes):
        shifted[key] = old_latest + delta * (minutes - key) / minutes
    for key in range(len(history)):
        shifted[key + minutes] = history.get(key, 0.0)
    result = MinuteArray({}, 0)
    result._data = shifted  # pylint: disable=protected-access
    return result


def set_export_window(my_predbat, force, now_minutes):
    """Set the inverter's programmed export window from the log's force export line, or clear it."""
    if force and force[0] == "True":
        start = int(force[1]) * 60 + int(force[2])
        end = int(force[3]) * 60 + int(force[4])
        # A window ending before it starts runs over midnight
        if end <= start:
            end += 24 * 60
        average = my_predbat.rate_export.get(start, 0) if my_predbat.rate_export else 0
        my_predbat.export_window = [{"start": start, "end": end, "average": average}]
        my_predbat.isExporting = start <= now_minutes < end
    else:
        my_predbat.export_window = []
        my_predbat.isExporting = False


def apply_run(my_predbat, prev, run):
    """Move the restored instance forwards from the previous run's moment to this run's."""
    gap = run["minutes_now"] - my_predbat.minutes_now
    if gap < 0:
        raise ValueError("Log run at minute {} is before the state at minute {}".format(run["minutes_now"], my_predbat.minutes_now))
    if run.get("today") and prev.get("today"):
        for name, now_value, prev_value in zip(COUNTER_ARRAYS, run["today"], prev["today"]):
            setattr(my_predbat, name, shift_counter(getattr(my_predbat, name), gap, max(float(now_value) - float(prev_value), 0.0)))
    elif gap:
        for name in COUNTER_ARRAYS:
            setattr(my_predbat, name, shift_counter(getattr(my_predbat, name), gap, 0.0))
    my_predbat.minutes_now = run["minutes_now"]
    my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=gap)
    my_predbat.now_utc_real = my_predbat.now_utc
    soc_kw, soc_percent, battery_power = run["soc"]
    my_predbat.soc_kw = float(soc_kw)
    my_predbat.soc_percent = int(soc_percent)
    for inverter in my_predbat.inverters:
        inverter.soc_kw = float(soc_kw)
        inverter.soc_percent = int(soc_percent)
    if run.get("inday"):
        my_predbat.load_inday_adjustment = float(run["inday"]) / 100.0
    if run.get("cloud"):
        my_predbat.metric_cloud_coverage = float(run["cloud"])
    # The window the inverter is holding is the one the previous run programmed
    set_export_window(my_predbat, prev.get("force"), my_predbat.minutes_now)


def replay_forward(my_predbat, debug_file, log_file, until=None, quiet=False):
    """Restore debug_file, then step through log_file re-planning where the log did; return the comparison rows.

    Each row is a dict with the run's time, whether it re-planned, and the logged and replayed export windows.
    until is an optional HH:MM after which the replay stops.
    """
    restore_debug_state(my_predbat, debug_file)
    my_predbat.plan_valid = True
    rebuild_load_pv_models(my_predbat)
    until_minutes = None
    if until:
        until_minutes = int(until.split(":")[0]) * 60 + int(until.split(":")[1])

    # Only the yaml's own day, from the first run after the yaml was written
    day = my_predbat.now_utc.strftime("%Y-%m-%d")
    start = my_predbat.now_utc.strftime("%H:%M")
    runs = [run for run in parse_log(log_file) if run["time"][:10] == day and run["time"][11:16] > start]
    prev = {"today": None, "force": None}
    rows = []
    for run in runs:
        if until_minutes is not None and run["minutes_now"] > until_minutes:
            break
        apply_run(my_predbat, prev, run)
        row = {"time": run["time"][11:16], "minutes_now": run["minutes_now"], "soc_percent": int(run["soc"][1]), "replanned": run["filtered"] is not None, "logged": parse_windows(run["filtered"]) if run["filtered"] else None, "replayed": None}
        if row["replanned"]:
            # Candidate windows start at the current slot, so they move with the clock as fetch moves them
            rescan_rate_windows(my_predbat)
            rebuild_load_pv_models(my_predbat)
            pv_step = my_predbat.pv_forecast_minute_step
            load_step = my_predbat.load_minutes_step
            my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
            my_predbat.calculate_plan(recompute=True, publish=False)
            row["replayed"] = parse_windows(my_predbat.window_as_text(my_predbat.export_window_best, my_predbat.export_limits_best))
        rows.append(row)
        prev = run
        if not quiet and row["replanned"]:
            print(format_row(row))
    return rows


def first_window(windows):
    """Return the first window as a short string, or '-' if there is none."""
    if not windows:
        return "-"
    return "{}-{} @{:g}%".format(windows[0][0], windows[0][1], windows[0][3])


def format_row(row):
    """Format one replanned row: time, logged first window, replayed first window and whether the lists match."""
    return "{} logged {:<26} replay {:<26} {}".format(row["time"], first_window(row["logged"]), first_window(row["replayed"]), "same" if row["logged"] == row["replayed"] else "DIFF")


def hhmm_minutes(text):
    """Convert HH:MM to minutes past midnight."""
    return int(text[:2]) * 60 + int(text[3:5])


def export_mode_now(windows, minutes_now):
    """Return 'export', 'freeze' or None for the plan's instruction at minutes_now.

    A window's percent is its target SoC for a forced export, 99 for Freeze Export and 100 for idle, as
    window_as_text prints them. Only today's windows can cover the current minute, so a window whose end
    is before its start (one running past midnight) is treated as ending at midnight.
    """
    for start, end, _rate, percent in windows or []:
        start_minutes = hhmm_minutes(start)
        end_minutes = hhmm_minutes(end) or 24 * 60
        if end_minutes <= start_minutes:
            end_minutes = 24 * 60
        if start_minutes <= minutes_now < end_minutes:
            if percent >= 100:
                return None
            return "freeze" if percent >= 99 else "export"
    return None


def chart_replay(rows, filename, title="Replay"):
    """Chart a replay as a PNG: actual SoC against each plan's export target, and what each plan was doing.

    The live plan (from the log) and the replayed plan are each carried forward between re-plans, so every run
    shows the plan in force at that moment. The target is the SoC the first forced-export window aims for,
    on the same % scale as the SoC itself. Uses the Agg backend so it never opens a window.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colours = {"Live (log)": "#2a78d6", "Replay": "#eb6834"}
    ink, muted, grid = "#0b0b0b", "#52514e", "#e6e5e1"
    times = [row["minutes_now"] / 60.0 for row in rows]

    # Carry each plan forward from its last re-plan
    carried = {"Live (log)": None, "Replay": None}
    modes = {name: [] for name in carried}
    targets = {name: [] for name in carried}
    for row in rows:
        if row["replanned"]:
            carried["Live (log)"] = row["logged"]
            carried["Replay"] = row["replayed"]
        for name in carried:
            modes[name].append(export_mode_now(carried[name], row["minutes_now"]))
            forced = [window for window in carried[name] or [] if window[3] < 99]
            targets[name].append(forced[0][3] if forced else None)

    fig, (ax_soc, ax_mode) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.1]})
    fig.suptitle(title, color=ink, fontsize=13, x=0.06, ha="left")

    ax_soc.plot(times, [row["soc_percent"] for row in rows], color=ink, linewidth=2, label="Actual SoC")
    for name, colour in colours.items():
        ax_soc.step(times, [float("nan") if value is None else value for value in targets[name]], where="post", color=colour, linewidth=2, linestyle="--", label="{} export target".format(name))
    ax_soc.set_ylabel("Battery SoC (%)", color=muted)
    ax_soc.set_ylim(0, 105)
    ax_soc.legend(frameon=False, loc="lower left")

    # One lane per plan: solid for forced export, hatched for Freeze Export
    step = (times[1] - times[0]) if len(times) > 1 else 5 / 60.0
    for lane, name in enumerate(colours):
        for hour, mode in zip(times, modes[name]):
            if mode:
                ax_mode.add_patch(plt.Rectangle((hour, lane + 0.15), step, 0.7, facecolor=colours[name] if mode == "export" else "none", edgecolor=colours[name], hatch=None if mode == "export" else "////", linewidth=0))
    ax_mode.set_yticks([0.5, 1.5], list(colours))
    ax_mode.set_ylim(0, 2)
    ax_mode.set_ylabel("Plan says\nexport now", color=muted)
    ax_mode.set_xlabel("Time of day (hour)", color=muted)
    ax_mode.text(1.0, 1.02, "solid = forced export, hatched = freeze", transform=ax_mode.transAxes, ha="right", va="bottom", color=muted, fontsize=8)

    for axis in (ax_soc, ax_mode):
        axis.grid(True, color=grid, linewidth=0.8)
        axis.set_axisbelow(True)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        axis.tick_params(colors=muted)
    fig.tight_layout()
    fig.savefig(filename, dpi=110)
    plt.close(fig)


def summarise(rows):
    """Return (replanned, identical, same_first_start) counts for a replay.

    identical counts re-plans whose whole export window list matches the log; same_first_start is the looser
    count where only the first window's start agrees, which is the part that decides whether export is on now.
    """
    replanned = [row for row in rows if row["replanned"]]
    identical = [row for row in replanned if row["logged"] == row["replayed"]]
    same_start = [row for row in replanned if first_window(row["logged"]).split("-")[0] == first_window(row["replayed"]).split("-")[0]]
    return len(replanned), len(identical), len(same_start)
