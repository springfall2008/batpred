# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for pv_calibration - the shrunk PV calibration fit, the P10/P90 band and the evidence store helpers."""

import math
from datetime import date, datetime, timedelta, timezone

from const import PV_CALIBRATION_HIGHEST
from pv_calibration import band_scaling, clean_pv_calibration_evidence, day_age, evidence_peak_kw, fit_pv_calibration, merge_pv_calibration_evidence, pv_calibration_scan_length, pv_day

TODAY = "2026-11-16"
SLOT = 30


def day_before(days):
    """The date this many days before TODAY."""
    return (date.fromisoformat(TODAY) - timedelta(days=days)).isoformat()


def bell_kwh(minute, total_kwh):
    """kWh in the half hour starting at minute for a bell-shaped day of total_kwh (06:00-18:00, peak at noon)."""
    centre = minute + SLOT / 2
    if centre < 360 or centre > 1080:
        return 0.0
    shape = math.sin(math.pi * (centre - 360) / 720)
    return shape * total_kwh * math.pi / 2 / 24  # 24 half hours across the 12-hour day; sin integrates to 2/pi of its peak


def make_day(ratio, peak=4.0, scale=1.0, noise=None, total_kwh=20.0):
    """An evidence day: a bell-shaped forecast of total_kwh x scale and actual = forecast x ratio (x noise[minute] where given)."""
    slots = {}
    for minute in range(0, 1440, SLOT):
        forecast = bell_kwh(minute, total_kwh) * scale
        factor = ratio * (noise.get(minute, 1.0) if noise else 1.0)
        slots[minute] = [forecast, forecast * factor]
    return {"slots": slots, "peak_kw": peak, "clipped_minutes": 0}


def fit(evidence, pv_scaling=1.0, shrink_day=0.20, shrink_slot=0.20, half_life=3.5):
    """fit_pv_calibration with the default settings."""
    return fit_pv_calibration(evidence, TODAY, pv_scaling, half_life, shrink_day, shrink_slot, SLOT)


def check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def test_fit_steady_bias():
    """28 days at 0.8 calibrate to 0.8 overall and in every daytime slot."""
    model = fit({day_before(k): make_day(0.8) for k in range(1, 29)})
    failed = check("steady total", model is not None and abs(model["total"] - 0.8) < 0.03, model and model["total"])
    for minute in range(420, 1020, SLOT):
        failed |= check("steady slot {}".format(minute), abs(model["adj"][minute] - 0.8) < 0.03, model["adj"][minute])
    return failed


def test_fit_reports_day_to_day_spread():
    """spread_day is the weighted standard deviation of the per-day log ratios, not the standard error of their mean."""
    model = fit({day_before(k): make_day(0.8 if k % 2 else 1.25) for k in range(1, 29)})
    expected = abs(math.log(1.25) - math.log(0.8)) / 2  # alternate days either side of the mean
    failed = check("spread present", model is not None and "spread_day" in model, model and list(model))
    failed |= check("spread value", model is not None and abs(model.get("spread_day", 0) - expected) < 0.03, model and (model.get("spread_day"), expected))
    failed |= check("spread above se", model is not None and model.get("spread_day", 0) > 3 * model["se_day"], model and (model.get("spread_day"), model["se_day"]))
    return failed


def test_fit_calibrated_noise_stays_near_one():
    """Day ratios alternating 0.6 / 1.4 (mean 1, no consistent bias) leave the factor near 1."""
    model = fit({day_before(k): make_day(0.6 if k % 2 else 1.4) for k in range(1, 29)})
    return check("noise total near 1", model is not None and abs(math.log(model["total"])) < 0.05, model and model["total"])


def test_fit_large_tau_is_ratio_of_sums():
    """With no shrinkage the day factor is the recency-weighted ratio of sums."""
    evidence = {day_before(k): make_day(0.6 + 0.02 * k) for k in range(1, 29)}
    model = fit(evidence, shrink_day=1e6, shrink_slot=1e6)
    sa = sf = 0.0
    for day, record in evidence.items():
        w = 0.5 ** ((day_age(day, TODAY) - 1) / 3.5)
        sf += w * sum(f for f, _ in record["slots"].values())
        sa += w * sum(a for _, a in record["slots"].values())
    return check("ratio of sums", model is not None and abs(model["total"] - sa / sf) < 1e-6, (model and model["total"], sa / sf))


def test_fit_down_days_excluded():
    """Days whose actual is under 10% of the forecast are down days and do not move the factor."""
    evidence = {day_before(k): make_day(0.8) for k in range(1, 11)}
    for k in range(11, 16):
        evidence[day_before(k)] = make_day(0.05)
    model = fit(evidence)
    failed = check("down days listed", model is not None and sorted(model["down_days"]) == sorted(day_before(k) for k in range(11, 16)), model and model["down_days"])
    failed |= check("down days ignored", abs(model["total"] - 0.8) < 0.03, model["total"])
    return failed


def test_fit_needs_three_days():
    """Two valid days are not enough to calibrate."""
    return check("two days", fit({day_before(1): make_day(0.8), day_before(2): make_day(0.8)}) is None)


def test_fit_pv_scaling_applied():
    """The stored forecast is before pv_scaling, so halving pv_scaling doubles the factor."""
    model = fit({day_before(k): make_day(0.8) for k in range(1, 29)}, pv_scaling=0.5)
    return check("pv_scaling", model is not None and abs(model["total"] - 1.6) < 0.03, model and model["total"])


def test_fit_sparse_slot_uses_day_factor():
    """A slot with a usable forecast on only 2 days takes the day factor."""
    evidence = {day_before(k): make_day(0.8) for k in range(1, 29)}
    for k in range(1, 29):
        evidence[day_before(k)]["slots"][300] = [0.05 if k <= 2 else 0.0, 0.2 if k <= 2 else 0.0]
    model = fit(evidence)
    return check("sparse slot", model is not None and model["adj"][300] == model["total"], model and (model["adj"][300], model["total"]))


def test_fit_clamps():
    """A huge ratio is held to PV_CALIBRATION_HIGHEST."""
    model = fit({day_before(k): make_day(10.0) for k in range(1, 29)})
    return check("clamp", model is not None and model["total"] <= PV_CALIBRATION_HIGHEST and max(model["adj"].values()) <= PV_CALIBRATION_HIGHEST, model and model["total"])


def test_band_matches_old_arithmetic():
    """Worst, best and average over the last 7 days follow the old solcast.py arithmetic."""
    ratios = {1: 0.5, 2: 0.7, 3: 0.9, 4: 1.1, 5: 0.8, 6: 1.0, 7: 0.6}
    evidence = {day_before(k): make_day(r) for k, r in ratios.items()}
    evidence[day_before(9)] = make_day(2.0)  # outside the 7-day window
    worst, best, average = band_scaling(evidence, TODAY, 1.0, 7)
    total_f = sum(f for f, _ in make_day(1.0)["slots"].values())
    wa = sum(max(1.0 - 0.1 * (k - 1), 0.3) * total_f * r for k, r in ratios.items())
    wf = sum(max(1.0 - 0.1 * (k - 1), 0.3) * total_f for k in ratios)
    expect_average = min(max(wa / wf, 0.1), 2.0)
    expect_worst = min(max(min(ratios.values()) / expect_average, 0.3), 1.0)
    expect_best = min(max(ratios.values()) / expect_average, 2.0)
    failed = check("band average", abs(average - expect_average) < 1e-3, (average, expect_average))
    failed |= check("band worst", abs(worst - expect_worst) < 1e-3, (worst, expect_worst))
    failed |= check("band best", abs(best - expect_best) < 1e-3, (best, expect_best))
    failed |= check("band empty", band_scaling({}, TODAY, 1.0, 7)[:2] == (None, None))
    return failed


def test_peak():
    """The peak comes from the window of recent days only."""
    evidence = {day_before(20): make_day(0.9, peak=6.0), day_before(2): make_day(0.9, peak=3.0), day_before(5): make_day(0.9, peak=3.2)}
    return check("peak", evidence_peak_kw(evidence, TODAY, 7, 1.0) == 3.2, evidence_peak_kw(evidence, TODAY, 7, 1.0))


def test_peak_skips_down_days():
    """A down day's measured peak is not evidence of what the array produces, as before the store."""
    evidence = {day_before(2): make_day(0.9, peak=3.0), day_before(3): make_day(0.05, peak=4.5)}
    return check("peak down day", evidence_peak_kw(evidence, TODAY, 7, 1.0) == 3.0, evidence_peak_kw(evidence, TODAY, 7, 1.0))


def test_merge_replaces_and_ages():
    """A scanned day replaces the stored copy; days past max age are dropped."""
    stored = {day_before(30): make_day(0.9), day_before(5): make_day(0.9)}
    scanned = {day_before(5): make_day(0.7), day_before(1): make_day(0.8)}
    merged = merge_pv_calibration_evidence(stored, scanned, TODAY, 28)
    failed = check("merge keys", sorted(merged) == sorted([day_before(5), day_before(1)]), sorted(merged))
    failed |= check("merge replaced", merged[day_before(5)] is scanned[day_before(5)])
    return failed


def test_merge_drops_today_and_future():
    """A day dated today or later never counts as evidence."""
    tomorrow = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
    merged = merge_pv_calibration_evidence({tomorrow: make_day(0.9)}, {TODAY: make_day(0.9), day_before(1): make_day(0.9)}, TODAY, 28)
    return check("future dropped", sorted(merged) == [day_before(1)], sorted(merged))


def test_clean_drops_malformed():
    """A stored document keeps only well-formed days, with int minute keys."""
    good = {"slots": {"720": [1.0, 0.9], "750": [1.1, 1.0]}, "peak_kw": 3.0, "clipped_minutes": 0}
    data = {day_before(1): good, day_before(2): {"slots": {"720": "bad"}}, day_before(3): "text", "not-a-date": good, day_before(4): {"slots": []}}
    cleaned = clean_pv_calibration_evidence(data)
    failed = check("clean keys", sorted(cleaned) == [day_before(1)], sorted(cleaned))
    failed |= check("clean ints", cleaned.get(day_before(1), {}).get("slots", {}).get(720) == [1.0, 0.9], cleaned)
    failed |= check("clean non-dict", clean_pv_calibration_evidence(None) == {} and clean_pv_calibration_evidence([1, 2]) == {})
    clipped = {day_before(1): {"slots": {"720": [1.0, 0.8, 0.5, 0.4]}, "peak_kw": 3.0, "clipped_minutes": 30}, day_before(2): {"slots": {"720": [1.0, 0.8, 0.5]}}}
    cleaned = clean_pv_calibration_evidence(clipped)
    failed |= check("clean clipped slot kept", cleaned.get(day_before(1), {}).get("slots", {}).get(720) == [1.0, 0.8, 0.5, 0.4], cleaned)
    failed |= check("clean three numbers dropped", day_before(2) not in cleaned, sorted(cleaned))
    return failed


def test_scan_length():
    """First scan reads the history days; later ones the days since the last success plus one, capped."""
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    failed = check("scan none", pv_calibration_scan_length(None, now, 28, 2) == 28)
    failed |= check("scan 1 day", pv_calibration_scan_length(now - timedelta(days=1), now, 28, 2) == 2)
    failed |= check("scan 5 days", pv_calibration_scan_length(now - timedelta(days=5), now, 28, 2) == 6)
    failed |= check("scan 40 days", pv_calibration_scan_length(now - timedelta(days=40), now, 28, 2) == 28)
    return failed


def test_pv_day():
    """pv_day is the local midnight-to-midnight date of a minute of history."""
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    failed = check("pv_day now", pv_day(now, 0, timezone.utc) == "2026-11-16")
    failed |= check("pv_day 11h59 ago", pv_day(now, 11 * 60 + 59, timezone.utc) == "2026-11-16")
    failed |= check("pv_day 12h01 ago", pv_day(now, 12 * 60 + 1, timezone.utc) == "2026-11-15")
    failed |= check("day_age", day_age(day_before(1), TODAY) == 1 and day_age(day_before(28), TODAY) == 28)
    return failed


def run_pv_calibration_tests(my_predbat=None):
    """Run the PV calibration fit tests."""
    failed = False
    for test in (
        test_fit_steady_bias,
        test_fit_calibrated_noise_stays_near_one,
        test_fit_reports_day_to_day_spread,
        test_fit_large_tau_is_ratio_of_sums,
        test_fit_down_days_excluded,
        test_fit_needs_three_days,
        test_fit_pv_scaling_applied,
        test_fit_sparse_slot_uses_day_factor,
        test_fit_clamps,
        test_band_matches_old_arithmetic,
        test_peak,
        test_peak_skips_down_days,
        test_merge_replaces_and_ages,
        test_merge_drops_today_and_future,
        test_clean_drops_malformed,
        test_scan_length,
        test_pv_day,
    ):
        print("**** {} ****".format(test.__name__))
        failed = test() or failed
    return failed
