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

Newer Predbat versions also write "Replay input:" lines carrying what the human-readable lines round or leave out:
the load and PV forecasts, the plan's starting state at full precision, and the rates whenever they change. Where a
run has them they take precedence.

The replay carries on across midnight (roll_over_midnight), so a late-evening yaml replays the whole next day.
"""
import array
import ast
import math
import re
from datetime import date, timedelta

from const import PREDICT_STEP, EXPORT_MODE_TARGET
from utils import MinuteArray, pack_export_limit
from prediction import Prediction
from tests.test_single_debug import restore_debug_state, rebuild_load_pv_models, rescan_rate_windows, apply_overrides

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
# The cumulative load forecast at each 5-minute step the plan builds and the minute after, exactly as the plan reads it
LOAD_EXACT_RE = re.compile(r"Replay input: load forecast, cumulative kWh at each 5-minute step and the minute after from (\d\d):(\d\d) (\[.*\])$")
PV_INPUT_RE = re.compile(r"Replay input: PV forecast changed, 30-minute kWh from (\d\d):(\d\d) p50 \[([^\]]*)\] p10 \[([^\]]*)\] p90 \[([^\]]*)\]")
# The plan's starting state, each value written so it reads back as the same float (or None)
STATE_INPUT_RE = re.compile(r"Replay input: state (.*)$")
# The rates from now, as change points, logged only when they change
RATES_INPUT_RE = re.compile(r"Replay input: rates changed, from (\d\d):(\d\d) (.*)$")
RATES_SERIES_RE = re.compile(r"(\w+) (\[\]|\[\[.*?\]\]) (\d+)")
# Rate series in the rates line and the instance attributes they replace
RATES_ATTRIBUTES = {"import": "rate_import", "export": "rate_export", "import_base": "rate_import_base", "export_base": "rate_export_base"}
# The PV forecast per minute from now to the end of the plan, exactly, as runs of [kWh per minute, minutes]
PV_EXACT_RE = re.compile(r"Replay input: PV forecast changed, per-minute kWh runs from (\d\d):(\d\d) p50 (\[.*?\]\]) p10 (\[.*?\]\]) p90 (\[.*?\]\])$")
COST_RE = re.compile(r"Today's energy total net .*?, cost (-?[\d.]+)")
IN_FORCE_RE = re.compile(r"Best export window (\[.*\])")
# Logged for every recompute, whatever triggered it (a sensor change too), despite its wording
RECOMPUTE_RE = re.compile(r"Will recompute the plan as it is invalid")
# Logged by calculate_plan only when the plan really is invalid, so the new plan is adopted without comparing it to the old
INVALID_RE = re.compile(r"Recompute, previous plan is invalid")
FORCE_RE = re.compile(r"Inverter 0 Adjust force export to (True|False), change times from \S+ - \S+ to (\d+):(\d+):\d+ - (\d+):(\d+):\d+")
WINDOW_RE = re.compile(r"(\d\d-\d\d) (\d\d):(\d\d):\d\d - (\d\d-\d\d) (\d\d):(\d\d):\d\d @ ([\d.]+)\S+ ([\d.]+)%")
# The four day counters in the order the log prints them, and the history arrays that mirror them
COUNTER_ARRAYS = ("load_minutes", "import_today", "export_today", "pv_today")
# Other backwards cumulative histories read alongside the load history (the load filter subtracts car and
# iBoost energy from it). The log has no per-run figure for these, so they age with nothing added - right
# while the car is not charging and iBoost is idle, which is the case the replay supports so far.
AGED_ARRAYS = ("car_charging_energy", "iboost_energy_today")
# The midnight run goes on to plan every tariff in the comparison list, logging a full plan for each; none of
# that is the live plan, so a run stops collecting once it starts
COMPARE_RE = re.compile(r"Starting comparison of tariffs")
# State held as minutes from today's midnight. Fetch rebuilds it every run against the current midnight, so when
# the replay crosses midnight it moves back a day. plan_last_updated_minutes is deliberately left alone: once it
# is later than minutes_now, calculate_plan forces the start-of-day re-plan exactly as the live system does.
DAY_KEYED_DICTS = (
    "rate_import",
    "rate_export",
    "rate_gas",
    "rate_import_base",
    "rate_export_base",
    "rate_import_no_io",
    "rate_import_replicated",
    "rate_export_replicated",
    "rate_gas_replicated",
    "rate_min_forward",
    "rate_export_max_forward",
    "future_energy_rates_import",
    "future_energy_rates_export",
    "manual_import_rates",
    "manual_export_rates",
    "pv_forecast_minute",
    "pv_forecast_minute10",
    "pv_forecast_minute90",
    "pv_light_dark",
    "load_scaling_dynamic",
    "dynamic_load_baseline",
    "carbon_intensity",
    "alert_active_keep",
    "manual_soc_keep",
    "manual_soc_max_keep",
    "all_active_keep",
    "all_active_keep_max",
)
DAY_WINDOW_LISTS = ("charge_window", "export_window", "charge_window_best", "export_window_best", "low_rates", "high_export_rates", "iboost_plan")
DAY_MINUTE_LISTS = ("manual_charge_times", "manual_export_times", "manual_freeze_charge_times", "manual_freeze_export_times", "manual_demand_times", "manual_all_times")
WINDOW_MINUTE_KEYS = ("start", "end", "start_orig", "end_orig")


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
            if run is None or run.get("comparing"):
                continue
            if COMPARE_RE.search(line):
                run["comparing"] = True
                continue
            # A recomputing run prints the re-plan's fresh window list before any plan in force, so has none to read
            if RECOMPUTE_RE.search(line):
                run["recompute"] = True
                continue
            # A run that finds the plan invalid (the previous run's adoption was overridden, as when the 8-hourly
            # config refresh is pending) adopts its re-plan without comparing it to the old plan
            if INVALID_RE.search(line):
                run["invalid"] = True
                continue
            # The first "Best export window" of a run is the plan in force when it starts - the one the previous
            # run adopted. Later ones in the same run are the re-plan's working lists.
            if run["in_force"] is None and not run.get("recompute"):
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
                (LOAD_EXACT_RE, "load_exact"),
                (PV_INPUT_RE, "pv_input"),
                (PV_EXACT_RE, "pv_exact"),
                (STATE_INPUT_RE, "state"),
                (RATES_INPUT_RE, "rates"),
                (FILTERED_RE, "filtered"),
                (FORCE_RE, "force"),
            ):
                # A run logs the inverter's SoC again after executing the plan; the plan started from the first
                if store == "soc" and run.get("soc"):
                    continue
                found = regex.search(line)
                if found:
                    if store == "state":
                        run[store] = parse_state(found.group(1))
                    elif store == "rates":
                        run[store] = parse_rates(found.groups())
                    else:
                        run[store] = found.groups() if store in ("soc", "today", "force", "next_limit", "load_input", "load_exact", "pv_input", "pv_exact") else found.group(1)
    kept = []
    rates = None
    for run in runs:
        # Rates are logged only when they change, so a change logged by a dropped run still holds for the runs after it
        rates = run.get("rates") or rates
        if run.get("soc"):
            if rates and not run.get("rates"):
                run["rates"] = rates
            rates = None
            kept.append(run)
    return kept


def parse_state(text):
    """Parse the name value pairs of a "Replay input: state" line into a dict of floats, None where the log had none."""
    tokens = text.split()
    return {name: None if value == "None" else float(value) for name, value in zip(tokens[::2], tokens[1::2])}


def parse_rates(groups):
    """Parse a "Replay input: rates changed" line into (start minute, {series: (change points, end)}).

    Each series is a list of [minutes from start, rate] at every change of rate, followed by the minute from start it
    runs to.
    """
    hours, minutes, text = groups
    series = {}
    for name, points, end in RATES_SERIES_RE.findall(text):
        series[name] = ([(int(offset), float(rate)) for offset, rate in ast.literal_eval(points)], int(end))
    return int(hours) * 60 + int(minutes), series


def today_values(run):
    """Return a run's four day counters (load, import, export, PV), exact from its state line when it has one, else None."""
    state = run.get("state")
    if state and all(state.get(name) is not None for name in ("load_today", "import_today", "export_today", "pv_today")):
        return (state["load_today"], state["import_today"], state["export_today"], state["pv_today"])
    return run.get("today")


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
        # A window ending before it starts runs over midnight; seen from the small hours, it started yesterday
        if end <= start:
            end += 24 * 60
            if now_minutes < start - 12 * 60:
                start -= 24 * 60
                end -= 24 * 60
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


def counter_gain(now_value, prev_value, new_day=False):
    """Return the kWh a day counter gained between two runs.

    The counters restart at midnight, so across one the reading itself is the gain since midnight; what came in
    between the last run before midnight and midnight itself is lost - at most one run's worth. Within a day a
    reading below the previous one is sensor noise, and counts as no gain.
    """
    now_value, prev_value = float(now_value), float(prev_value)
    if new_day:
        return now_value
    return max(now_value - prev_value, 0.0)


def crosses_midnight(prev, run):
    """Return True if run is on a later day than prev, the run (or the yaml's moment) before it."""
    return run["time"][:10] != prev["time"][:10]


def shift_cumulative(data, minutes):
    """Move a cumulative-from-midnight series back by minutes, so it counts from the later midnight instead."""
    base = 0.0
    for minute in sorted(data):
        if minute > minutes:
            break
        base = data[minute]
    return {minute - minutes: value - base for minute, value in data.items()}


def shift_windows(windows, minutes):
    """Move each window's minutes back by minutes, returning new windows and leaving the originals alone."""
    shifted = []
    for window in windows:
        window = dict(window)
        for key in WINDOW_MINUTE_KEYS:
            if isinstance(window.get(key), int):
                window[key] -= minutes
        shifted.append(window)
    return shifted


def roll_over_midnight(my_predbat, days=1):
    """Move the instance into a later day: everything held as minutes from midnight moves back a day per day.

    The clock (now_utc) is left where it is and minutes_now goes negative, so apply_run then advances it across
    midnight in the new day's minutes. Predbat counts wall-clock minutes from midnight, so a day is always 1440
    minutes, even when the clocks change.

    Only what the yaml carried moves; nothing new arrives. Rates the live system fetched since - the next day-ahead
    prices, say - come from the log's rates lines (apply_logged_rates); without them the plan sees the yaml's rates
    running out a day sooner.
    """
    minutes = days * 24 * 60
    for name in DAY_KEYED_DICTS:
        data = getattr(my_predbat, name, None)
        if data:
            setattr(my_predbat, name, {key - minutes: value for key, value in data.items()})
    if my_predbat.load_forecast:
        my_predbat.load_forecast = shift_cumulative(my_predbat.load_forecast, minutes)
    my_predbat.load_forecast_array = [shift_cumulative(forecast, minutes) for forecast in (my_predbat.load_forecast_array or [])]
    for name in DAY_WINDOW_LISTS:
        windows = getattr(my_predbat, name, None)
        if windows:
            setattr(my_predbat, name, shift_windows(windows, minutes))
    my_predbat.car_charging_slots = [shift_windows(slots, minutes) for slots in (my_predbat.car_charging_slots or [])]
    for name in DAY_MINUTE_LISTS:
        values = getattr(my_predbat, name, None)
        if values:
            setattr(my_predbat, name, [value - minutes for value in values])
    my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=days)
    my_predbat.minutes_now -= minutes


def apply_run(my_predbat, prev, run):
    """Move the restored instance forwards from the previous run's moment to this run's."""
    gap = run["minutes_now"] - my_predbat.minutes_now
    if gap < 0:
        raise ValueError("Log run at minute {} is before the state at minute {}".format(run["minutes_now"], my_predbat.minutes_now))
    now_today, prev_today = today_values(run), today_values(prev)
    if now_today and prev_today:
        for name, now_value, prev_value in zip(COUNTER_ARRAYS, now_today, prev_today):
            setattr(my_predbat, name, shift_counter(getattr(my_predbat, name), gap, counter_gain(now_value, prev_value, crosses_midnight(prev, run))))
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
    if now_today:
        my_predbat.load_minutes_now, my_predbat.import_today_now, my_predbat.export_today_now, my_predbat.pv_today_now = (float(value) for value in now_today)
    if run.get("rates"):
        apply_logged_rates(my_predbat, run["rates"])
    if run.get("pv_exact"):
        apply_logged_pv_exact(my_predbat, run["pv_exact"])
    elif run.get("pv_input"):
        apply_logged_pv_forecast(my_predbat, run["pv_input"])
    # Fetch clears these every run, so the plan's stale-p90 guard only ever compares within one cycle. Left set,
    # a PV update that moves p50 but not p90 reads as a p90 left behind, and the plan replaces p90 with p50
    my_predbat.pv_forecast_minute90_signatures = None
    if run.get("inday"):
        my_predbat.load_inday_adjustment = float(run["inday"]) / 100.0
    # calculate_plan recomputes the load divergence from the load history, which the replay can only rebuild
    # at the log's 5-minute resolution - smoother than the live 1-minute history, so it comes out different.
    # Use the logged value instead; it is rounded to 2 dp of the fraction exactly as get_load_divergence returns it.
    if run.get("divergence"):
        my_predbat.replay_load_divergence = round(float(run["divergence"]) / 100.0, 2)
    apply_logged_state(my_predbat, run.get("state"))
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
    a different pv_metric90_weight (--override).
    """
    restore_debug_state(my_predbat, debug_file)
    apply_overrides(my_predbat, overrides)
    my_predbat.plan_valid = True
    rebuild_load_pv_models(my_predbat)

    # Every run is placed in minutes from the yaml day's midnight, so runs on later days follow on from it
    start_date = my_predbat.now_utc.date()
    plan_day = my_predbat.now_utc.strftime("%d-%m")
    start = my_predbat.minutes_now
    runs = []
    for run in parse_log(log_file):
        run["minute"] = (date.fromisoformat(run["time"][:10]) - start_date).days * 24 * 60 + run["minutes_now"]
        if run["minute"] > start:
            runs.append(run)
    until_minutes = None
    if until:
        until_minutes = int(until.split(":")[0]) * 60 + int(until.split(":")[1])
        # A time no later than the yaml's means that time the next day
        if until_minutes <= start:
            until_minutes += 24 * 60
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
    # The yaml's own moment stands in for the run before the first one
    prev = {"today": None, "force": None, "time": my_predbat.now_utc.isoformat(sep=" ")}
    rows = []
    # A faithful replay needs the code that wrote the log, so past a version change it can only be expected to
    # match loosely. Carry on, but mark every row from the change onwards so results can be judged separately.
    change = version_change(runs)
    change_index = None
    if change is not None:
        change_index, before, after = change
        print("Replay note: the log changes from Predbat {} to {} at {}; plans after that are not expected to match as closely".format(before, after, runs[change_index]["time"][11:16]))
    # Minutes from the yaml day's midnight to the instance's own midnight
    day_start = 0
    for index, run in enumerate(runs):
        if until_minutes is not None and run["minute"] > until_minutes:
            break
        next_run = runs[index + 1] if index + 1 < len(runs) else None
        if run["minute"] - day_start >= 24 * 60:
            days = (run["minute"] - day_start) // (24 * 60)
            roll_over_midnight(my_predbat, days)
            day_start += days * 24 * 60
            if not quiet:
                print("Replay: rolled over midnight into {}".format(run["time"][:10]))
        if simulate:
            before = today_values(prev) or yaml_today
            now_today = today_values(run) or before
            new_day = crosses_midnight(prev, run)
            load_kwh = counter_gain(now_today[0], before[0], new_day)
            pv_kwh = counter_gain(now_today[3], before[3], new_day)
            rebuild_load_pv_models(my_predbat)
            sim_soc = simulate_soc(my_predbat, sim_soc, run["minutes_now"] - my_predbat.minutes_now, pv_kwh, load_kwh)
        apply_run(my_predbat, prev, run)
        # Live rebuilds the load forecast every run, re-plan or not, so the instance holds what live held at each run
        refresh_load_forecast(my_predbat, run)
        if simulate:
            my_predbat.soc_kw = sim_soc
            my_predbat.soc_percent = int(round(sim_soc / my_predbat.soc_max * 100)) if my_predbat.soc_max else 0
            for inverter in my_predbat.inverters:
                inverter.soc_kw = sim_soc
                inverter.soc_percent = my_predbat.soc_percent
        row = {
            "time": run["time"][11:16],
            # From the yaml day's midnight, like the windows below, so rows after midnight carry on from it
            "minutes_now": run["minute"],
            "soc_percent": int(run["soc"][1]),
            "soc_sim_percent": my_predbat.soc_percent if simulate else None,
            "replanned": run["filtered"] is not None,
            "logged": None,
            "replayed": None,
            "logged_candidate": parse_windows(run["filtered"], plan_day) if run["filtered"] else None,
            "replayed_candidate": None,
            "after_version_change": change_index is not None and index >= change_index,
        }
        # Marks the replay's own log the way the live log marks each run, so the two can be read side by side
        my_predbat.log("--------------- Replay of run at {} minutes now {}".format(run["time"], my_predbat.minutes_now))
        if row["replanned"]:
            # Candidate windows start at the current slot, so they move with the clock as fetch moves them
            rescan_rate_stats(my_predbat)
            rescan_rate_windows(my_predbat)
            rebuild_load_pv_models(my_predbat)
            pv_step = my_predbat.pv_forecast_minute_step
            load_step = my_predbat.load_minutes_step
            my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
            if run.get("invalid"):
                # The live run found its plan invalid, so it adopted the new plan without comparing it to the old
                my_predbat.plan_valid = False
            candidate = capture_candidate(my_predbat)
            row["replayed_candidate"] = parse_windows(candidate, plan_day) if candidate else None
            adopted = parse_windows(my_predbat.window_as_text(my_predbat.export_window_best, my_predbat.export_limits_best), plan_day)
            # The log shows the adopted plan at the start of the next run, with windows that ended by then dropped
            if next_run and next_run["in_force"] is not None:
                row["logged"] = parse_windows(next_run["in_force"], plan_day)
                row["replayed"] = [window for window in adopted if window[1] > next_run["minute"]]
        rows.append(row)
        prev = run
        if not quiet and row["replanned"]:
            print(format_row(row))
    return rows


def rescan_rate_stats(my_predbat):
    """Recompute the rate min/max/average and the forward-looking rate curves for the current time, as fetch does every run.

    They are taken over the horizon from now, so they move as the clock does: a peak that has passed drops out
    of rate_max, which changes the charge/export thresholds and the battery value. The rates themselves are
    the final ones the yaml holds, which is what fetch's last scan reads too.
    """
    if my_predbat.rate_import:
        my_predbat.rate_scan(my_predbat.rate_import, print=True)
    if my_predbat.rate_import_base:
        my_predbat.rate_min_base, my_predbat.rate_max_base, _, _, _ = my_predbat.rate_minmax(my_predbat.rate_import_base)
    if my_predbat.rate_export:
        my_predbat.rate_scan_export(my_predbat.rate_export, print=False)
    if my_predbat.rate_export_base:
        my_predbat.rate_export_max_forward = my_predbat.rate_export_max_forward_calc(my_predbat.rate_export_base)


def refresh_load_forecast(my_predbat, run):
    """Bring the load forecast up to this run: from the log's load line where it has one, else rebuilt from history."""
    rebuild_load_forecast(my_predbat)
    if run.get("load_exact"):
        # A log that records the load forecast exactly as the plan reads it makes the rebuild unnecessary
        apply_logged_load_exact(my_predbat, run["load_exact"])
    elif run.get("load_input"):
        # Older logs give it per 5-minute slot, rounded
        apply_logged_load_forecast(my_predbat, run["load_input"])


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


def apply_logged_load_exact(my_predbat, load_exact):
    """Set the load forecast from the run's logged "Replay input: load forecast, cumulative kWh" line.

    The line gives, for each 5-minute step from the logged start, load_forecast at the step's start and the minute
    after - the two values step_data_history() reads - so those are set exactly, and removed where the log gave
    None. Other minutes keep their values; the plan does not read them.
    """
    hours, minutes, text = load_exact
    start = int(hours) * 60 + int(minutes)
    forecast = dict(my_predbat.load_forecast or {})
    for index, values in enumerate(ast.literal_eval(text)):
        for offset, value in enumerate(values):
            minute = start + index * PREDICT_STEP + offset
            if value is None:
                forecast.pop(minute, None)
            else:
                forecast[minute] = float(value)
    my_predbat.load_forecast = forecast


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


def apply_logged_state(my_predbat, state):
    """Set the plan's starting values from a run's "Replay input: state" line, which the other lines round.

    The day counters are taken through today_values; a value the log gave as None is left alone.
    """
    if not state:
        return
    if state.get("soc_kw") is not None:
        my_predbat.soc_kw = state["soc_kw"]
        for inverter in my_predbat.inverters:
            inverter.soc_kw = state["soc_kw"]
    if state.get("soc_max") is not None:
        my_predbat.soc_max = state["soc_max"]
    if state.get("inday") is not None:
        my_predbat.load_inday_adjustment = state["inday"]
    if state.get("cost_today") is not None:
        my_predbat.cost_today_sofar = state["cost_today"]


def apply_logged_rates(my_predbat, rates_input):
    """Replace the rates from a logged "Replay input: rates changed" line.

    Each series is rebuilt per minute from its change points over the span the log gave, and ends where the live
    rates ended. Minutes before the logged start keep their values; only the cost so far reads them, and that comes
    from the log. A line carried over from a run before midnight is in that day's minutes, so moves back a day.
    """
    start, series = rates_input
    if start > my_predbat.minutes_now:
        start -= 24 * 60
    for name, (points, end) in series.items():
        attribute = RATES_ATTRIBUTES.get(name)
        if attribute is None:
            continue
        rates = {minute: value for minute, value in (getattr(my_predbat, attribute, None) or {}).items() if minute < start}
        for index, (offset, rate) in enumerate(points):
            until = points[index + 1][0] if index + 1 < len(points) else end
            for minute in range(start + offset, start + until):
                rates[minute] = rate
        setattr(my_predbat, attribute, rates)


def apply_logged_pv_exact(my_predbat, pv_exact):
    """Set the PV forecasts from a logged "Replay input: PV forecast changed, per-minute kWh runs" line.

    Each series is given per minute from the logged start to the end of the plan as runs of [kWh, minutes], so the
    minutes it covers are set exactly, and removed where the log gave None. Minutes outside it keep their values.
    """
    hours, minutes = pv_exact[0], pv_exact[1]
    start = int(hours) * 60 + int(minutes)
    for name, text in zip(("pv_forecast_minute", "pv_forecast_minute10", "pv_forecast_minute90"), pv_exact[2:]):
        series = dict(getattr(my_predbat, name) or {})
        minute = start
        for value, count in ast.literal_eval(text):
            for _ in range(count):
                if value is None:
                    series.pop(minute, None)
                else:
                    series[minute] = float(value)
                minute += 1
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
    # Hours count on from the yaml day's midnight, so a replay that crosses midnight wraps back to 0
    ax_mode.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda hour, _position: "{:g}".format(hour % 24)))
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
