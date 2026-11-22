# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
#

"""Prospective household-load forecast scores and an optional, conservative source guard.

Evidence is recorded before consumption occurs. Both sources are scored over the same
UTC intervals; completed hourly windows overlap, but switching uses windows issued
at least a day apart. This module never trains a model or edits its history.
"""

import math

HORIZONS = (60, 480, 1440)
RETENTION_MINUTES = 7 * 1440
STORE_VERSION = 2
AVAILABILITY_GRACE_MINUTES = 120
TRANSIENT_FORECAST_PROBLEMS = ("ml_unavailable", "model_unavailable", "model_unknown", "model_not_initialized", "stale_or_short_forecast")


def valid_energy(value):
    """Whether a value is finite, numeric and non-negative (booleans are not energy)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def forecast_energy(data, start, duration):
    """Sum a dense cumulative forecast, handling daily resets without padding gaps."""
    total = 0.0
    for minute in range(start, start + duration):
        before, after = data.get(minute, None), data.get(minute + 1, None)
        if not valid_energy(before) or not valid_energy(after):
            return None
        # Match Fetch.get_from_incrementing: a midnight reset (including a
        # smoothed downward ramp) contributes no artificial consumption.
        total += max(after - before, 0.0)
    return round(total, 6)


class LoadForecastGuard:
    """Bounded forecast snapshots, real-world errors and a daily fallback latch."""

    def __init__(self, fingerprint):
        """Start empty; the fingerprint prevents comparing incompatible load definitions."""
        self.fingerprint = fingerprint
        self.records = []
        # Automatic planning starts on classic; a valid model alone earns no permission.
        self.fallback = True
        self.trigger_horizon = None
        self.blocked_since = None
        self.last_capture = None
        self.last_now = None
        self.last_message = None
        self.loaded = False
        self.empty_reads = []
        self.last_save = None
        self.dirty = False
        self.unavailable_since = None

    def restore(self, data, now):
        """Restore compatible, bounded evidence; malformed records never authorise ML."""
        if not isinstance(data, dict) or data.get("version") not in (1, STORE_VERSION) or data.get("fingerprint") != self.fingerprint:
            return
        records = data.get("records")
        if not isinstance(records, list):
            return
        cleaned = {}
        for record in records[-1000:]:
            if not isinstance(record, dict):
                continue
            start, duration = record.get("start"), record.get("duration")
            if type(start) is not int or type(duration) is not int or duration not in HORIZONS or not now - RETENTION_MINUTES <= start <= now:
                continue
            if not valid_energy(record.get("ml")) or not valid_energy(record.get("classic")):
                continue
            actual = record.get("actual")
            if actual is not None and (not valid_energy(actual) or start + duration > now):
                continue
            cleaned[(start, duration)] = {key: record.get(key) for key in ("start", "duration", "ml", "classic", "actual")}
        # Live snapshots collected during a storage outage win over stored copies.
        for record in self.records:
            cleaned[(record["start"], record["duration"])] = record
        self.records = sorted(cleaned.values(), key=lambda r: (r["start"], r["duration"]))[-600:]
        # Version 1 allowed ML before evidence existed. Keep its observations,
        # but never migrate that implicit permission into the new policy.
        self.fallback = not (data.get("version") == STORE_VERSION and data.get("fallback") is False)
        self.trigger_horizon = data.get("trigger_horizon") if data.get("trigger_horizon") in (480, 1440) else None
        blocked_since = data.get("blocked_since")
        self.blocked_since = blocked_since if type(blocked_since) is int and blocked_since <= now else None
        unavailable_since = data.get("unavailable_since")
        self.unavailable_since = unavailable_since if type(unavailable_since) is int and unavailable_since <= now else None
        if type(unavailable_since) is int and unavailable_since > now:
            self.require_new_evidence(now)
        self.last_capture = max((r["start"] // 60 for r in self.records), default=None)

    def serialise(self):
        """Return JSON-safe evidence for the Storage component, separate from ML files."""
        return {"version": STORE_VERSION, "fingerprint": self.fingerprint, "records": self.records, "fallback": self.fallback, "trigger_horizon": self.trigger_horizon, "blocked_since": self.blocked_since, "unavailable_since": self.unavailable_since}

    def update_availability(self, now, available, problem="ml_unavailable"):
        """Manage earned permission separately from the current forecast's usability."""
        previous = self.unavailable_since
        if available:
            if previous is not None and (now < previous or now - previous >= AVAILABILITY_GRACE_MINUTES):
                self.require_new_evidence(now)
            self.unavailable_since = None
        elif problem in TRANSIENT_FORECAST_PROBLEMS:
            if previous is None or previous > now:
                self.unavailable_since = now
                if previous is not None:
                    self.require_new_evidence(now)
            if now - self.unavailable_since >= AVAILABILITY_GRACE_MINUTES:
                self.require_new_evidence(now)
        else:
            self.unavailable_since = None
            self.require_new_evidence(now)
        self.dirty |= previous != self.unavailable_since

    def require_new_evidence(self, now):
        """Withdraw permission; forecasts issued before this event cannot earn it back."""
        if not self.fallback or self.blocked_since is None or self.blocked_since > now:
            self.blocked_since = now
            self.dirty = True
        self.dirty |= not self.fallback
        self.fallback = True

    def advance(self, now, actual_reader):
        """Score newly elapsed windows once, skipping windows with missing actuals."""
        if self.last_now is not None and now < self.last_now:
            # A backwards clock jump must not leave future evidence or a capture lock.
            self.records = [r for r in self.records if r["start"] <= now and (r["actual"] is None or r["start"] + r["duration"] <= now)]
            self.last_capture = None
            self.require_new_evidence(now)
        self.last_now = now
        kept = [r for r in self.records if now - RETENTION_MINUTES <= r["start"] <= now]
        self.dirty |= len(kept) != len(self.records)
        self.records = kept
        for record in self.records:
            if record["actual"] is None and record["start"] + record["duration"] <= now:
                actual = actual_reader(record["start"], record["duration"])
                if valid_energy(actual):
                    record["actual"] = round(actual, 6)
                    self.dirty = True

    def capture(self, now, minute_now, ml, classic):
        """Freeze at most one pair per hour, without replacing a previous prediction."""
        if self.last_capture == now // 60:
            return
        added = False
        for duration in HORIZONS:
            ml_energy = forecast_energy(ml, minute_now, duration)
            classic_energy = forecast_energy(classic, minute_now, duration)
            if ml_energy is None or classic_energy is None:
                continue
            self.records.append({"start": now, "duration": duration, "ml": ml_energy, "classic": classic_energy, "actual": None})
            added = True
        if added:
            self.last_capture = now // 60
            self.dirty = True

    def scores(self):
        """Rolling energy MAE and signed bias, reported separately for each lead window."""
        scores = {}
        for duration in HORIZONS:
            rows = [r for r in self.records if r["duration"] == duration and r["actual"] is not None]
            count = len(rows)
            scores[str(duration // 60) + "h"] = {
                "samples": count,
                "ml_mae_kwh": round(sum(abs(r["ml"] - r["actual"]) for r in rows) / count, 4) if count else None,
                "classic_mae_kwh": round(sum(abs(r["classic"] - r["actual"]) for r in rows) / count, 4) if count else None,
                "ml_bias_kwh": round(sum(r["ml"] - r["actual"] for r in rows) / count, 4) if count else None,
                "classic_bias_kwh": round(sum(r["classic"] - r["actual"] for r in rows) / count, 4) if count else None,
            }
        return scores

    def judge(self, now, min_days=3, threshold_kwh=3.0, threshold_percent=40.0):
        """Earn ML selection on both horizons; retain classic until clear daily wins exist."""
        decisions = {}
        qualifying = {}
        for duration in (480, 1440):
            daily = []
            for record in sorted(self.records, key=lambda r: r["start"], reverse=True):
                if record["duration"] != duration or record["actual"] is None:
                    continue
                if not daily or daily[-1]["start"] - record["start"] >= 1440:
                    daily.append(record)
                if len(daily) == min_days:
                    break
            # Old successes cannot clear the latch after an outage. Recovery
            # requires a recent complete set, not scattered good days in a week.
            recent = len(daily) >= min_days and now - (daily[0]["start"] + duration) <= 180 and daily[0]["start"] - daily[-1]["start"] <= min_days * 1440
            if not recent:
                continue
            bad, wins = [], []
            scale = duration / 1440
            for row in daily:
                ml_error, classic_error = abs(row["ml"] - row["actual"]), abs(row["classic"] - row["actual"])
                bad.append(ml_error > classic_error * 1.10 + 0.25 * scale)
                within_limits = ml_error <= threshold_kwh * scale or ml_error <= row["actual"] * threshold_percent / 100
                fresh = self.blocked_since is None or row["start"] >= self.blocked_since
                wins.append(fresh and within_limits and ml_error <= classic_error * 0.80 and classic_error - ml_error >= 0.25 * scale)
            decisions[duration] = "bad" if all(bad) else "mixed"
            qualifying[duration] = all(wins)
        previous = (self.fallback, self.trigger_horizon)
        failing = [duration for duration, decision in decisions.items() if decision == "bad"]
        if failing:
            self.require_new_evidence(now)
            self.trigger_horizon = min(failing)
        elif len(decisions) < 2:
            # An outage or missing comparisons cannot leave an earned grant in force.
            if not self.fallback:
                self.require_new_evidence(now)
                self.trigger_horizon = None
        elif self.fallback and all(qualifying.values()):
            self.fallback = False
            self.trigger_horizon = None
            self.blocked_since = None
        self.dirty |= previous != (self.fallback, self.trigger_horizon)
        if not self.fallback:
            return "ml_earned"
        return "ml_worse_than_classic" if self.trigger_horizon else "measuring" if len(decisions) < 2 else "historical_preferred"
