# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""
PV calibration from a store of daily evidence.

Each stored day holds, per plan slot, the uncalibrated forecast (before pv_scaling) and the actual generation (with
inverter clipping imputed), plus the day's measured peak. The fit turns the last few weeks of that into a day factor and a
per-slot shape, each shrunk toward 1 by its own day-to-day noise: a forecast that is already calibrated per site scatters
around 1 with no consistent direction, so it is left almost alone, while a real system error (shading, a misconfigured
array, inverter losses) is the same day after day and survives. With no shrinkage the fit is the recency-weighted ratio of
sums the 7-day calibration used, over a longer window.

Pure functions only - no Predbat state - so the arithmetic is unit tested directly (tests/test_pv_calibration.py).
"""

import math
from datetime import date, timedelta

from const import (
    PV_CALIBRATION_HIGHEST,
    PV_CALIBRATION_LOG_CLAMP,
    PV_CALIBRATION_LOWEST,
    PV_CALIBRATION_MIN_DAYS,
    PV_CALIBRATION_MIN_SLOT_KWH,
    PV_CALIBRATION_MIN_SLOT_POINTS,
)


def pv_day(now_utc, minutes_ago, local_tz):
    """The local date (midnight to midnight, "YYYY-MM-DD") of the point minutes_ago before now_utc."""
    moment = now_utc - timedelta(minutes=minutes_ago)
    if local_tz is not None:
        moment = moment.astimezone(local_tz)
    return moment.date().isoformat()


def day_age(day, today):
    """Whole days from day to today: yesterday is 1."""
    return (date.fromisoformat(today) - date.fromisoformat(day)).days


def clamp(value, low, high):
    """value held within [low, high]."""
    return min(max(value, low), high)


def _valid_date(day):
    """True for a "YYYY-MM-DD" string."""
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return isinstance(day, str)


def clean_pv_calibration_evidence(data):
    """Evidence read back from storage with anything malformed dropped: {day: {"slots": {minute: [forecast, actual]}, "peak_kw", "clipped_minutes"}}."""
    cleaned = {}
    if not isinstance(data, dict):
        return cleaned
    for day, record in data.items():
        if not _valid_date(day) or not isinstance(record, dict) or not isinstance(record.get("slots"), dict):
            continue
        slots = {}
        good = True
        for minute, pair in record["slots"].items():
            try:
                minute_int = int(minute)
                values = [float(value) for value in pair]
            except (TypeError, ValueError, KeyError):
                good = False
                break
            if not 0 <= minute_int < 24 * 60 or len(values) not in (2, 4) or min(values) < 0:
                good = False
                break
            slots[minute_int] = values
        if not good or not slots:
            continue
        try:
            peak = max(float(record.get("peak_kw", 0) or 0), 0.0)
            clipped = max(int(record.get("clipped_minutes", 0) or 0), 0)
        except (TypeError, ValueError):
            continue
        cleaned[day] = {"slots": slots, "peak_kw": peak, "clipped_minutes": clipped}
    return cleaned


def merge_pv_calibration_evidence(stored, scanned, today, max_age_days):
    """Stored evidence with each scanned day replacing its stored copy; days dated today or later, or older than max_age_days, are dropped."""
    merged = {}
    for source in (stored or {}, scanned or {}):
        for day, record in source.items():
            if not _valid_date(day):
                continue
            age = day_age(day, today)
            if 1 <= age <= max_age_days:
                merged[day] = record
    return merged


def pv_calibration_scan_length(last_success, now, history_days, scan_days):
    """Days a scan reads: history_days until one has succeeded, then the days since the last success plus a day's overlap, capped."""
    if last_success is None:
        return history_days
    gap_days = int((now - last_success).total_seconds() / (24 * 60 * 60) + 0.5) + 1
    return min(max(scan_days, gap_days), history_days)


def slot_actual(pair, pv_scaling):
    """
    A slot's actual kWh with inverter clipping imputed at the current pv_scaling. A slot is [forecast, actual], or for a slot
    with clipped minutes [forecast, measured, clipped minutes' forecast, clipped minutes' measured] (forecasts before
    pv_scaling): a clipped minute counts as the forecast when that is above what was measured, as impute_clipped_pv_history
    does, so a later pv_scaling change applies to clipped minutes too.
    """
    if len(pair) >= 4:
        return pair[1] - pair[3] + max(pair[2] * pv_scaling, pair[3])
    return pair[1]


def _day_totals(record, pv_scaling):
    """(forecast kWh x pv_scaling, actual kWh) summed over a day's slots."""
    forecast = sum(pair[0] for pair in record["slots"].values()) * pv_scaling
    actual = sum(slot_actual(pair, pv_scaling) for pair in record["slots"].values())
    return forecast, actual


def _is_down(forecast, actual):
    """A day with no useful forecast, or whose actual is under 10% of it (the system was probably down)."""
    return forecast < 0.1 or actual < 0.1 * forecast


def shrunk_log(values, prior_sd, center):
    """Log factor shrunk toward 0: centre x tau^2 / (tau^2 + se^2); se from the weighted spread of the per-day log ratios."""
    sw = sum(w for _, w in values)
    if sw <= 0 or len(values) < PV_CALIBRATION_MIN_SLOT_POINTS:
        return 0.0
    mean = sum(v * w for v, w in values) / sw
    var = sum(w * (v - mean) ** 2 for v, w in values) / sw
    n_eff = sw * sw / sum(w * w for _, w in values)
    se2 = var / max(n_eff - 1, 1)
    return center * prior_sd**2 / (prior_sd**2 + se2)


def _standard_error(values):
    """Standard error of the weighted mean of values [(value, weight)], as used by shrunk_log."""
    sw = sum(w for _, w in values)
    if sw <= 0 or len(values) < 2:
        return 0.0
    mean = sum(v * w for v, w in values) / sw
    var = sum(w * (v - mean) ** 2 for v, w in values) / sw
    n_eff = sw * sw / sum(w * w for _, w in values)
    return math.sqrt(var / max(n_eff - 1, 1))


def _spread(values):
    """The weighted standard deviation of values [(value, weight)]: how far a single day typically lands from the mean."""
    sw = sum(w for _, w in values)
    if sw <= 0:
        return 0.0
    mean = sum(v * w for v, w in values) / sw
    return math.sqrt(sum(w * (v - mean) ** 2 for v, w in values) / sw)


def fit_pv_calibration(evidence, today, pv_scaling, half_life, shrink_day, shrink_slot, slot_minutes):
    """
    The day factor and per-slot shape from the evidence, each shrunk toward 1 by its own noise, or None with fewer than
    PV_CALIBRATION_MIN_DAYS valid days. adj and raw_adj hold a factor for every slot of the day; raw_* are unshrunk.
    """
    log_clamp = PV_CALIBRATION_LOG_CLAMP
    days = []
    down_days = []
    day_points = []
    for day in sorted(evidence or {}):
        age = day_age(day, today)
        if age < 1:
            continue
        record = evidence[day]
        forecast, actual = _day_totals(record, pv_scaling)
        weight = 0.5 ** ((age - 1) / half_life)
        down = _is_down(forecast, actual)
        day_points.append({"day": day, "forecast_kwh": forecast, "actual_kwh": actual, "ratio": actual / forecast if forecast > 0 else None, "weight": weight, "down": down})
        if down:
            down_days.append(day)
        else:
            days.append((day, weight, forecast, actual, record))
    if len(days) < PV_CALIBRATION_MIN_DAYS:
        return None

    # Day factor: centred on the ratio of weighted sums, shrunk by the spread of the per-day ratios (weighted by energy, so dull days count less)
    sa = sum(w * a for _, w, _, a, _ in days)
    sf = sum(w * f for _, w, f, _, _ in days)
    day_values = [(clamp(math.log(max(a, 0.01) / f), -log_clamp, log_clamp), w * f) for _, w, f, a, _ in days]
    center = math.log(max(sa, 0.01) / sf)
    raw_total = clamp(math.exp(center), PV_CALIBRATION_LOWEST, PV_CALIBRATION_HIGHEST)
    total = clamp(math.exp(shrunk_log(day_values, shrink_day, center)), PV_CALIBRATION_LOWEST, PV_CALIBRATION_HIGHEST)

    # Slot shape, relative to the day factor
    adj = {}
    raw_adj = {}
    slot_points = []
    for minute in range(0, 24 * 60, slot_minutes):
        values = []
        ssa = ssf = 0.0
        for day, w, _, _, record in days:
            pair = record["slots"].get(minute)
            if not pair:
                continue
            f = pair[0] * pv_scaling
            a = slot_actual(pair, pv_scaling)
            if f < PV_CALIBRATION_MIN_SLOT_KWH:
                continue
            values.append((clamp(math.log(max(a, 0.005) / f / total), -log_clamp, log_clamp), w * f))
            ssa += w * a
            ssf += w * f
            slot_points.append({"day": day, "minute": minute, "forecast_kwh": f, "actual_kwh": a, "ratio": a / f, "weight": w})
        if len(values) < PV_CALIBRATION_MIN_SLOT_POINTS or ssf <= 0:
            adj[minute] = total
            raw_adj[minute] = raw_total
            continue
        slot_center = math.log(max(ssa, 0.001) / ssf / total)
        raw_adj[minute] = clamp(total * math.exp(slot_center), PV_CALIBRATION_LOWEST, PV_CALIBRATION_HIGHEST)
        adj[minute] = clamp(total * math.exp(shrunk_log(values, shrink_slot, slot_center)), PV_CALIBRATION_LOWEST, PV_CALIBRATION_HIGHEST)

    return {
        "total": total,
        "raw_total": raw_total,
        "se_day": _standard_error(day_values),
        "spread_day": _spread(day_values),
        "adj": adj,
        "raw_adj": raw_adj,
        "days_used": sorted(day for day, _, _, _, _ in days),
        "down_days": down_days,
        "day_points": day_points,
        "slot_points": slot_points,
    }


def band_scaling(evidence, today, pv_scaling, band_days, min_days=1):
    """
    (worst, best, average) day scaling over the last band_days days, as the 7-day calibration worked them out: worst and
    best are the extreme day ratios divided by the weighted average, weights max(1 - 0.1 x (age - 1), 0.3). worst and best
    are None when the window holds fewer than min_days valid days (one day's ratio over the average is 1, a band of nothing).
    """
    valid = 0
    worst = best = None
    weighted_actual = weighted_forecast = 0.0
    for day in sorted(evidence or {}):
        age = day_age(day, today)
        if not 1 <= age <= band_days:
            continue
        forecast, actual = _day_totals(evidence[day], pv_scaling)
        if _is_down(forecast, actual):
            continue
        valid += 1
        ratio = actual / forecast
        worst = ratio if worst is None else min(worst, ratio)
        best = ratio if best is None else max(best, ratio)
        weight = max(1.0 - 0.1 * (age - 1), 0.3)
        weighted_actual += actual * weight
        weighted_forecast += forecast * weight
    average = clamp(weighted_actual / weighted_forecast if weighted_forecast > 0 else 1.0, 0.1, 2.0)
    if worst is None or valid < min_days:
        return None, None, average
    return clamp(worst / average, 0.3, 1.0), min(best / average, 2.0), average


def evidence_peak_kw(evidence, today, days, pv_scaling):
    """The largest measured peak (kW) over the days aged 1 to days, leaving out down days as the 7-day calibration did."""
    peak = 0.0
    for day, record in (evidence or {}).items():
        if 1 <= day_age(day, today) <= days and not _is_down(*_day_totals(record, pv_scaling)):
            peak = max(peak, record.get("peak_kw", 0) or 0)
    return peak
