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
in-day load adjustment, the load divergence, the cost so far today and the inverter's programmed export window. The PV forecast is
the one in the yaml - the log records only its total.
"""
import array
import math
import re
from datetime import date, timedelta

from const import PREDICT_STEP, EXPORT_MODE_TARGET
from utils import MinuteArray, pack_export_limit
from prediction import Prediction
from tests.test_single_debug import restore_debug_state, rebuild_load_pv_models, rescan_rate_windows

RUN_RE = re.compile(r"PredBat - update at (\S+ \S+) with clock skew .*minutes now (\d+)")
SOC_RE = re.compile(r"Inverter 0 SoC: ([\d.]+)kWh (\d+)%.*current battery power (-?\d+)W")
TODAY_RE = re.compile(r"Current data so far today: load ([\d.]+)kWh, import ([\d.]+)kWh, export ([\d.]+)kWh, PV ([\d.]+)kWh")
INDAY_RE = re.compile(r"in-day adjustment ([\d.]+)%")
DIVERGENCE_RE = re.compile(r"Load divergence over .* divergence ([\d.]+)%")
FILTERED_RE = re.compile(r"Export windows filtered (\[.*\])")
NEXT_LIMIT_RE = re.compile(r"Next export window will be: .* at reserve \((\d+), (\w+), ([\d.]+)\)")
VERSION_RE = re.compile(r"version (\S+) currently running")
# Lines written by Predbat versions that log the inputs a replay cannot otherwise recover
LOAD_INPUT_RE = re.compile(r"Replay input: load forecast, 5-minute Wh from (\d\d):(\d\d) \[([^\]]*)\]")
PV_INPUT_RE = re.compile(r"Replay input: PV forecast changed, 30-minute kWh from (\d\d):(\d\d) p50 \[([^\]]*)\] p10 \[([^\]]*)\] p90 \[([^\]]*)\]")
COST_RE = re.compile(r"Today's energy total net .*?, cost (-?[\d.]+)")
IN_FORCE_RE = re.compile(r"Best export window (\[.*\])")
FORCE_RE = re.compile(r"Inverter 0 Adjust force export to (True|False), change times from \S+ - \S+ to (\d+):(\d+):\d+ - (\d+):(\d+):\d+")
WINDOW_RE = re.compile(r"(\d\d-\d\d) (\d\d):(\d\d):\d\d - (\d\d-\d\d) (\d\d):(\d\d):\d\d @ ([\d.]+)\S+ ([\d.]+)%")
# The four day counters in the order the log prints them, and the history arrays that mirror them
COUNTER_ARRAYS = ("load_minutes", "import_today", "export_today", "pv_today")
# Other backwards cumulative histories read alongside the load history (the load filter subtracts car and
# iBoost energy from it). The log has no per-run figure for these, so they age with nothing added - right
# while the car is not charging and iBoost is idle, which is the case the replay supports so far.
AGED_ARRAYS = ("car_charging_energy", "iboost_energy_today")


def day_offset(date_text, day):
    """Days from day to date_text, both dd-mm (the plan text carries no year)."""
    if not day:
        return 0
    delta = (date(2000, int(date_text[3:5]), int(date_text[:2])) - date(2000, int(day[3:5]), int(day[:2]))).days
    # A plan seen on 31 Dec reaches into January
    if delta < -180:
        delta += 366
    return delta


def parse_windows(text, day=None):
    """Parse a window_as_text string into (start minute, end minute, rate, percent) tuples.

    Minutes are counted from midnight of day (dd-mm, the date of the run that logged or made the plan), so a
    window tomorrow starts at 1440 or later and can never be mistaken for one today.
    """
    windows = []
    for m in WINDOW_RE.findall(text):
        start = day_offset(m[0], day) * 1440 + int(m[1]) * 60 + int(m[2])
        end = day_offset(m[3], day) * 1440 + int(m[4]) * 60 + int(m[5])
        windows.append((start, end, float(m[6]), float(m[7])))
    return windows


def minute_label(minute):
    """Format a minute from midnight as HH:MM, marking later days with +Nd."""
    days, rest = divmod(minute, 1440)
    return "{:02d}:{:02d}{}".format(rest // 60, rest % 60, "+{}d".format(days) if days else "")


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
                run = {"time": marker.group(1), "minutes_now": int(marker.group(2)), "filtered": None, "force": None, "in_force": None}
                runs.append(run)
                continue
            if run is None:
                continue
            # The first "Best export window" of a run is the plan in force when it starts - the one the previous
            # run adopted. Later ones in the same run are the re-plan's working lists.
            if run["in_force"] is None:
                found = IN_FORCE_RE.search(line)
                if found:
                    run["in_force"] = found.group(1)
                    continue
            for regex, store in (
                (SOC_RE, "soc"),
                (TODAY_RE, "today"),
                (INDAY_RE, "inday"),
                (DIVERGENCE_RE, "divergence"),
                (COST_RE, "cost"),
                (NEXT_LIMIT_RE, "next_limit"),
                (VERSION_RE, "version"),
                (LOAD_INPUT_RE, "load_input"),
                (PV_INPUT_RE, "pv_input"),
                (FILTERED_RE, "filtered"),
                (FORCE_RE, "force"),
            ):
                found = regex.search(line)
                if found:
                    run[store] = found.groups() if store in ("soc", "today", "force", "next_limit", "load_input", "pv_input") else found.group(1)
    return [run for run in runs if run.get("soc")]


def shift_counter(history, minutes, delta):
    """Advance a backwards cumulative history by minutes, adding delta kWh over the gap.

    The history is indexed by minutes ago, with index 0 the latest counter and larger indexes older (smaller)
    values, so advancing time moves every existing entry to a higher index and the gap fills with a ramp from
    the old latest value up to the new one. The growth is spread evenly across the gap, which is all the log's
    once-per-run counters can tell us.
    """
    if minutes <= 0 or not history:
        return history
    old_latest = history.get(0, 0.0)
    if isinstance(history, dict):
        # A plain dict history may be sparse, so shift its keys rather than assuming a dense range
        result = {key + minutes: value for key, value in history.items()}
        for key in range(minutes):
            result[key] = old_latest + delta * (minutes - key) / minutes
        return result
    shifted = array.array("d", [0.0]) * (len(history) + minutes)
    for key in range(minutes):
        shifted[key] = old_latest + delta * (minutes - key) / minutes
    for key in range(len(history)):
        shifted[key + minutes] = history.get(key, 0.0)
    result = MinuteArray({}, 0)
    result._data = shifted  # pylint: disable=protected-access
    return result


def set_export_window(my_predbat, force, now_minutes, limit=None):
    """Set the inverter's programmed export window and its limit from the log, or clear them.

    force is the log's force export line; limit is the (mode, target, power) the log gave for the next export
    window. The window and its limit must stay the same length, as the base prediction reads them together.
    """
    if force and force[0] == "True":
        start = int(force[1]) * 60 + int(force[2])
        end = int(force[3]) * 60 + int(force[4])
        # A window ending before it starts runs over midnight
        if end <= start:
            end += 24 * 60
        average = my_predbat.rate_export.get(start, 0) if my_predbat.rate_export else 0
        my_predbat.export_window = [{"start": start, "end": end, "average": average}]
        if limit:
            mode, target, power = limit
            my_predbat.export_limits = [pack_export_limit(int(mode), None if target == "None" else int(target), float(power))]
        else:
            my_predbat.export_limits = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
        my_predbat.isExporting = start <= now_minutes < end
    else:
        my_predbat.export_window = []
        my_predbat.export_limits = []
        my_predbat.isExporting = False


def simulate_soc(my_predbat, soc_kw, minutes, pv_kwh, load_kwh):
    """Advance the battery by minutes from soc_kw under the plan in force, with the PV and load that actually happened.

    The battery and inverter are modelled by Predbat's own prediction engine, so charge/discharge rate curves,
    losses, reserve, the inverter AC limit (shared by PV and battery on a hybrid) and the export limit all apply
    exactly as the planner sees them. The plan's forecast for the gap is replaced by the actual PV and load,
    spread evenly over it. Must be called with the instance's clock still at the start of the gap and its
    stepped PV and load models built for that moment (rebuild_load_pv_models), which supply the rest of the horizon.
    """
    if minutes <= 0:
        return soc_kw
    pv_step = dict(my_predbat.pv_forecast_minute_step)
    load_step = dict(my_predbat.load_minutes_step)
    steps = max(minutes // PREDICT_STEP, 1)
    for index in range(steps):
        pv_step[index * PREDICT_STEP] = pv_kwh / steps
        load_step[index * PREDICT_STEP] = load_kwh / steps
    # The step just past the gap is simulated too, so it needs a value even where the model has none
    pv_step.setdefault(steps * PREDICT_STEP, 0.0)
    load_step.setdefault(steps * PREDICT_STEP, 0.0)
    prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step, soc_kw=soc_kw)
    # Only the gap is needed, so stop the simulation just after it rather than running the whole horizon
    prediction.forecast_minutes = (steps + 1) * PREDICT_STEP
    prediction.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=prediction.forecast_minutes, save="replay")
    return prediction.predict_soc.get(steps * PREDICT_STEP, soc_kw)


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
    for name in AGED_ARRAYS:
        history = getattr(my_predbat, name, None)
        if isinstance(history, list):
            # One history per car
            setattr(my_predbat, name, [shift_counter(car, gap, 0.0) for car in history])
        elif history:
            setattr(my_predbat, name, shift_counter(history, gap, 0.0))
    my_predbat.minutes_now = run["minutes_now"]
    my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=gap)
    my_predbat.now_utc_real = my_predbat.now_utc
    soc_kw, soc_percent, battery_power = run["soc"]
    my_predbat.soc_kw = float(soc_kw)
    my_predbat.soc_percent = int(soc_percent)
    for inverter in my_predbat.inverters:
        inverter.soc_kw = float(soc_kw)
        inverter.soc_percent = int(soc_percent)
    # Every prediction's metric starts from the cost so far today, and the prediction's day totals start from
    # these counters; the live system recomputes both each run, so take them from the log too
    if run.get("cost"):
        my_predbat.cost_today_sofar = float(run["cost"])
    if run.get("today"):
        my_predbat.load_minutes_now, my_predbat.import_today_now, my_predbat.export_today_now, my_predbat.pv_today_now = (float(value) for value in run["today"])
    if run.get("pv_input"):
        apply_logged_pv_forecast(my_predbat, run["pv_input"])
    if run.get("inday"):
        my_predbat.load_inday_adjustment = float(run["inday"]) / 100.0
    # calculate_plan recomputes the load divergence from the load history, which the replay can only rebuild
    # at the log's 5-minute resolution - smoother than the live 1-minute history, so it comes out different.
    # Use the logged value instead; it is rounded to 2 dp of the fraction exactly as get_load_divergence returns it.
    if run.get("divergence"):
        my_predbat.replay_load_divergence = round(float(run["divergence"]) / 100.0, 2)
    # The window the inverter is holding is the one the previous run programmed
    set_export_window(my_predbat, prev.get("force"), my_predbat.minutes_now, prev.get("next_limit"))


def replay_forward(my_predbat, debug_file, log_file, until=None, quiet=False, simulate=False, overrides=None):
    """Restore debug_file, then step through log_file re-planning where the log did; return the comparison rows.

    Each row is a dict with the run's time, whether it re-planned, the logged and replayed export windows, the
    actual SoC from the log and, when simulate is set, the replay's own SoC. until is an optional HH:MM after
    which the replay stops.

    With simulate the replay is closed-loop: the battery is stepped forward under the replayed plan using the
    actual PV and load (see simulate_soc), and that SoC - not the logged one - is what each re-plan starts from.
    Without it every re-plan starts from the SoC the log recorded.

    overrides maps instance attribute names to values set after the yaml is restored, for what-if replays such as
    a different pv_metric90_weight.
    """
    restore_debug_state(my_predbat, debug_file)
    for name, value in (overrides or {}).items():
        if not hasattr(my_predbat, name):
            raise ValueError("Unknown setting {} for --replay_set".format(name))
        print("Replay override: {} = {} (was {})".format(name, value, getattr(my_predbat, name)))
        setattr(my_predbat, name, value)
    my_predbat.plan_valid = True
    rebuild_load_pv_models(my_predbat)
    until_minutes = None
    if until:
        until_minutes = int(until.split(":")[0]) * 60 + int(until.split(":")[1])

    # Only the yaml's own day, from the first run after the yaml was written
    day = my_predbat.now_utc.strftime("%Y-%m-%d")
    plan_day = my_predbat.now_utc.strftime("%d-%m")
    start = my_predbat.now_utc.strftime("%H:%M")
    runs = [run for run in parse_log(log_file) if run["time"][:10] == day and run["time"][11:16] > start]
    # The yaml's own day counters stand in for the run before the first one
    yaml_today = (my_predbat.load_minutes_now, my_predbat.import_today_now, my_predbat.export_today_now, my_predbat.pv_today_now)
    install_logged_load_divergence(my_predbat)
    try:
        rows = replay_runs(my_predbat, runs, until_minutes, plan_day, yaml_today, simulate, quiet)
    finally:
        remove_logged_load_divergence(my_predbat)
    return rows


def install_logged_load_divergence(my_predbat):
    """Make get_load_divergence return the value the log recorded for the current run, when there is one."""
    original = my_predbat.get_load_divergence

    def logged_load_divergence(minutes_now, load_minutes):
        """Return the logged divergence, falling back to computing it when the log had none."""
        value = getattr(my_predbat, "replay_load_divergence", None)
        if value is None:
            return original(minutes_now, load_minutes)
        return value if my_predbat.metric_load_divergence_enable else None

    my_predbat.get_load_divergence = logged_load_divergence
    my_predbat.replay_load_divergence = None


def remove_logged_load_divergence(my_predbat):
    """Undo install_logged_load_divergence, so the shared instance is left as it was found."""
    for name in ("get_load_divergence", "replay_load_divergence"):
        if name in my_predbat.__dict__:
            del my_predbat.__dict__[name]


def parse_override(text):
    """Parse a name=value override, reading the value as a number or boolean when it looks like one."""
    name, _, value = text.partition("=")
    if not name or not _:
        raise ValueError("Expected name=value, got {}".format(text))
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return name.strip(), lowered == "true"
    try:
        return name.strip(), int(value)
    except ValueError:
        pass
    try:
        return name.strip(), float(value)
    except ValueError:
        return name.strip(), value


def version_change(runs):
    """Return (index, old version, new version) for the first run whose logged version differs, or None."""
    version = None
    for index, run in enumerate(runs):
        if not run.get("version"):
            continue
        if version is None:
            version = run["version"]
        elif run["version"] != version:
            return index, version, run["version"]
    return None


def replay_runs(my_predbat, runs, until_minutes, plan_day, yaml_today, simulate, quiet):
    """Step through the runs, re-planning where the log did, and return the comparison rows."""
    sim_soc = my_predbat.soc_kw
    prev = {"today": None, "force": None}
    rows = []
    # A faithful replay needs the code that wrote the log, so past a version change it can only be expected to
    # match loosely. Carry on, but mark every row from the change onwards so results can be judged separately.
    change = version_change(runs)
    change_index = None
    if change is not None:
        change_index, before, after = change
        print("Replay note: the log changes from Predbat {} to {} at {}; plans after that are not expected to match as closely".format(before, after, runs[change_index]["time"][11:16]))
    for index, run in enumerate(runs):
        if until_minutes is not None and run["minutes_now"] > until_minutes:
            break
        next_run = runs[index + 1] if index + 1 < len(runs) else None
        if simulate:
            before = prev.get("today") or yaml_today
            now_today = run.get("today") or before
            load_kwh = max(float(now_today[0]) - float(before[0]), 0.0)
            pv_kwh = max(float(now_today[3]) - float(before[3]), 0.0)
            rebuild_load_pv_models(my_predbat)
            sim_soc = simulate_soc(my_predbat, sim_soc, run["minutes_now"] - my_predbat.minutes_now, pv_kwh, load_kwh)
        apply_run(my_predbat, prev, run)
        if simulate:
            my_predbat.soc_kw = sim_soc
            my_predbat.soc_percent = int(round(sim_soc / my_predbat.soc_max * 100)) if my_predbat.soc_max else 0
            for inverter in my_predbat.inverters:
                inverter.soc_kw = sim_soc
                inverter.soc_percent = my_predbat.soc_percent
        row = {
            "time": run["time"][11:16],
            "minutes_now": run["minutes_now"],
            "soc_percent": int(run["soc"][1]),
            "soc_sim_percent": my_predbat.soc_percent if simulate else None,
            "replanned": run["filtered"] is not None,
            "logged": None,
            "replayed": None,
            "logged_candidate": parse_windows(run["filtered"], plan_day) if run["filtered"] else None,
            "replayed_candidate": None,
            "after_version_change": change_index is not None and index >= change_index,
        }
        if row["replanned"]:
            # Candidate windows start at the current slot, so they move with the clock as fetch moves them
            rescan_rate_windows(my_predbat)
            rebuild_load_forecast(my_predbat)
            if run.get("load_input"):
                # A log that records the load forecast the plan used makes the rebuild unnecessary
                apply_logged_load_forecast(my_predbat, run["load_input"])
            rebuild_load_pv_models(my_predbat)
            pv_step = my_predbat.pv_forecast_minute_step
            load_step = my_predbat.load_minutes_step
            my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
            candidate = capture_candidate(my_predbat)
            row["replayed_candidate"] = parse_windows(candidate, plan_day) if candidate else None
            adopted = parse_windows(my_predbat.window_as_text(my_predbat.export_window_best, my_predbat.export_limits_best), plan_day)
            # The log shows the adopted plan at the start of the next run, with windows that ended by then dropped
            if next_run and next_run["in_force"] is not None:
                row["logged"] = parse_windows(next_run["in_force"], plan_day)
                row["replayed"] = [window for window in adopted if window[1] > next_run["minutes_now"]]
        rows.append(row)
        prev = run
        if not quiet and row["replanned"]:
            print(format_row(row))
    return rows


def rebuild_load_forecast(my_predbat):
    """Rebuild the weighted-bucket historical load forecast for the current time, as fetch does every run.

    Only for installs using it (days_previous_auto) and no other load forecast source, which is the case the
    replay supports so far: the forecast is then this alone. Without it every re-plan keeps the yaml's forecast.
    """
    if not my_predbat.load_forecast_history or my_predbat.load_minutes_age < 1:
        return
    forecast = my_predbat.compute_load_forecast_history(my_predbat.now_utc)
    if forecast:
        my_predbat.load_forecast = dict(forecast)


def parse_values(text):
    """Parse a logged comma-separated list of numbers, empty when the list was."""
    return [float(value) for value in text.split(",") if value.strip()]


def apply_logged_load_forecast(my_predbat, load_input):
    """Replace the load forecast from the run's logged "Replay input: load forecast" line.

    The log gives Wh per 5 minutes from a slot start; load_forecast is cumulative kWh from midnight, so the
    logged slots are stacked onto the forecast's own value at that start, spread evenly within each slot.
    Minutes before the start keep their values, as the plan only reads them for the in-day comparison.
    """
    hours, minutes, text = load_input
    start = int(hours) * 60 + int(minutes)
    forecast = my_predbat.load_forecast or {}
    base = 0.0
    for minute in sorted(forecast):
        if minute > start:
            break
        base = forecast[minute]
    rebuilt = {minute: value for minute, value in forecast.items() if minute < start}
    total = base
    for index, wh in enumerate(parse_values(text)):
        kwh = wh / 1000.0
        for offset in range(PREDICT_STEP):
            rebuilt[start + index * PREDICT_STEP + offset] = total + kwh * offset / PREDICT_STEP
        total += kwh
    rebuilt[start + len(parse_values(text)) * PREDICT_STEP] = total
    my_predbat.load_forecast = rebuilt


def apply_logged_pv_forecast(my_predbat, pv_input):
    """Update the PV forecasts from a logged "Replay input: PV forecast changed" line.

    Each series is logged as kWh per half hour from a half-hour start, which is spread evenly over the minutes of
    its half hour. Minutes outside the logged span keep their values; a series logged empty is left alone.
    """
    hours, minutes = pv_input[0], pv_input[1]
    start = int(hours) * 60 + int(minutes)
    for name, text in zip(("pv_forecast_minute", "pv_forecast_minute10", "pv_forecast_minute90"), pv_input[2:]):
        values = parse_values(text)
        if not values:
            continue
        series = dict(getattr(my_predbat, name) or {})
        for index, kwh in enumerate(values):
            for offset in range(30):
                series[start + index * 30 + offset] = kwh / 30.0
        setattr(my_predbat, name, series)


def capture_candidate(my_predbat):
    """Re-plan, returning the candidate plan's window text that calculate_plan logs before deciding whether to adopt it."""
    captured = []
    original = my_predbat.log

    def log(message, *args, **kwargs):
        """Keep the candidate plan line, then log as normal."""
        found = FILTERED_RE.search(str(message))
        if found:
            captured.append(found.group(1))
        return original(message, *args, **kwargs)

    my_predbat.log = log
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        del my_predbat.log
    return captured[-1] if captured else None


def first_window(windows):
    """Return the first window as a short string, or '-' if there is none."""
    if not windows:
        return "-"
    return "{}-{} @{:g}%".format(minute_label(windows[0][0]), minute_label(windows[0][1]), windows[0][3])


def format_row(row):
    """Format one replanned row: time, logged first window, replayed first window and whether the lists match."""
    adopted = "same" if row["logged"] == row["replayed"] else "DIFF"
    candidate = "same" if row.get("logged_candidate") == row.get("replayed_candidate") else "DIFF"
    return "{} adopted: logged {:<26} replay {:<26} {}   candidate {}".format(row["time"], first_window(row["logged"]), first_window(row["replayed"]), adopted, candidate)


def export_mode_now(windows, minutes_now):
    """Return 'export', 'freeze' or None for the plan's instruction at minutes_now.

    A window's percent is its target SoC for a forced export, 99 for Freeze Export and 100 for idle, as
    window_as_text prints them.
    """
    for start, end, _rate, percent in windows or []:
        if start <= minutes_now < end:
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
        if row["replanned"] and row["logged"] is not None:
            carried["Live (log)"] = row["logged"]
            carried["Replay"] = row["replayed"]
        for name in carried:
            modes[name].append(export_mode_now(carried[name], row["minutes_now"]))
            forced = [window for window in carried[name] or [] if window[3] < 99]
            targets[name].append(forced[0][3] if forced else None)

    fig, (ax_soc, ax_mode) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.1]})
    fig.suptitle(title, color=ink, fontsize=13, x=0.06, ha="left")

    ax_soc.plot(times, [row["soc_percent"] for row in rows], color=ink, linewidth=2, label="Actual SoC (log)")
    if any(row.get("soc_sim_percent") is not None for row in rows):
        ax_soc.plot(times, [row.get("soc_sim_percent") for row in rows], color=colours["Replay"], linewidth=2, label="Replay simulated SoC")
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

    changes = [hour for row, hour in zip(rows, times) if row.get("after_version_change")]
    if changes:
        for axis in (ax_soc, ax_mode):
            axis.axvline(changes[0], color=muted, linestyle=":", linewidth=1.5)
        ax_soc.text(changes[0], 102, " version change", color=muted, fontsize=8, va="bottom")
    for axis in (ax_soc, ax_mode):
        axis.grid(True, color=grid, linewidth=0.8)
        axis.set_axisbelow(True)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        axis.tick_params(colors=muted)
    fig.tight_layout()
    fig.savefig(filename, dpi=110)
    plt.close(fig)


def soc_rms_error(rows):
    """Root-mean-square difference in SoC % between the replay's simulated battery and the log, or None when not simulating."""
    errors = [row["soc_sim_percent"] - row["soc_percent"] for row in rows if row.get("soc_sim_percent") is not None]
    if not errors:
        return None
    return math.sqrt(sum(error * error for error in errors) / len(errors))


def summarise(rows, logged="logged", replayed="replayed", after_version_change=False):
    """Return (compared, identical, same_first_start) counts for a replay.

    By default this compares the adopted plans; pass logged="logged_candidate", replayed="replayed_candidate" to
    compare the candidate plans each re-plan produced before deciding whether to adopt them. identical counts
    re-plans whose whole export window list matches the log; same_first_start is the looser count where only the
    first window's start agrees, which is the part that decides whether export is on now. Rows from a logged
    version change onwards are counted only when after_version_change is set, as they are not expected to match.
    """
    replanned = [row for row in rows if row["replanned"] and row.get(logged) is not None and row.get("after_version_change", False) == after_version_change]
    identical = [row for row in replanned if row[logged] == row[replayed]]
    same_start = [row for row in replanned if first_window(row[logged]).split("-")[0] == first_window(row[replayed]).split("-")[0]]
    return len(replanned), len(identical), len(same_start)
