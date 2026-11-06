"""Conditional absent-EV demand for native prediction, never a charger plan or permission."""

from car_charging_config import read_car_charging_config

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from const import PREDICT_STEP
from car_charging_recurring import next_departure, recurring_enabled, resolve_recurring_session


def _signature(base):
    """Invalidate cached assumptions when the user changes policy, target, rate or ready time."""
    values = [read_car_charging_config(base), base.args.get("timezone"), base.car_charging_limit[0], base.car_charging_battery_size[0], base.car_charging_rate[0], base.car_charging_loss, base.car_charging_plan_time[0], base.forecast_minutes]
    return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _eligible(base, now):
    """Check absence and idle authority first, without reading any vehicle SOC or energy."""
    from car_charging_plan import _read_entity_state

    settings = read_car_charging_config(base)
    if not recurring_enabled(settings) or settings.get("execution_authorised") is not True or settings.get("household_fallback") is not True:
        return False
    if base.num_cars != 1 or base.car_charging_slots[0] or base.car_charging_planned[0] or base.car_charging_now_active(0) or base.car_charging_manual_soc[0] or base.car_charging_soc[0] != 0:
        return False
    # Native SOC zero is the recurring path's inactive placeholder, NOT a reading.
    for key, state in (("home_entity", "not_home"), ("permission_entity", "on"), ("relay_entity", "off")):
        item = _read_entity_state(base, settings.get(key))
        if item is None or item["state"] != state or max(item["last_changed"], item["last_updated"]) > now:
            return False
    return True


def _trips(history, now):
    """Use paired real home/away transitions; force updates and unknown gaps create no trips."""
    from car_charging_plan import _parse_entity_timestamp

    if not isinstance(history, list) or len(history) != 1 or not isinstance(history[0], list) or len(history[0]) > 50000:
        raise ValueError("arrival_history_invalid")
    records = []
    for item in history[0]:
        stamp = _parse_entity_timestamp(item["last_changed"], "arrival_history_time")
        if stamp > now:
            raise ValueError("arrival_history_future")
        if stamp >= now - timedelta(days=14):
            records.append((stamp, item["state"]))
    previous = None
    previous_at = None
    left = None
    trips = []
    for stamp, state in sorted(records):
        if previous_at == stamp and previous != state:
            raise ValueError("arrival_history_ambiguous")
        if state == previous:
            previous_at = stamp
            continue
        if state == "not_home" and previous == "home":
            left = stamp
        elif state == "home" and previous == "not_home" and left is not None:
            duration = (stamp - left).total_seconds()
            if 60 <= duration <= 86400:
                trips.append((left, duration))
            left = None
        else:
            left = None
        previous, previous_at = state, stamp
    return trips, left if previous == "not_home" else None


def _quantile(values, fraction):
    """Interpolate a descriptive empirical percentile, not a guaranteed arrival bound."""
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _arrival_samples(trips, left, now, zone, deadline):
    """Condition on trip duration, falling back to genuine past daily return clocks when sparse."""
    local = left.astimezone(zone)
    elapsed = (now - left).total_seconds()
    surviving = [(start, duration) for start, duration in trips if duration > elapsed]
    if len(surviving) >= 3:
        similar = [(start, duration) for start, duration in surviving if abs((start.astimezone(zone).hour - local.hour + 12) % 24 - 12) <= 3]
        cohort = "all_remaining_trips"
        if len(similar) >= 3:
            surviving, cohort = similar, "similar_departure_hours"
            same_day_type = [(start, duration) for start, duration in similar if (start.astimezone(zone).weekday() < 5) == (local.weekday() < 5)]
            if len(same_day_type) >= 3:
                surviving, cohort = same_day_type, "similar_hours_and_day_type"
        return [(left + timedelta(seconds=duration) - now).total_seconds() for start, duration in surviving], cohort
    # Long trips can outlast most short-trip examples. Do not turn a sparse tail
    # into a precise ETA: use one last genuine return per completed local day.
    last_returns = {}
    today = now.astimezone(zone).date()
    for start, duration in trips:
        returned = (start + timedelta(seconds=duration)).astimezone(zone)
        if returned.date() < today:
            last_returns[returned.date()] = max(returned, last_returns.get(returned.date(), returned))
    arrivals = []
    for returned in last_returns.values():
        candidate = datetime.combine(today, returned.time(), tzinfo=zone)
        if candidate <= now.astimezone(zone):
            candidate = datetime.combine(today + timedelta(days=1), returned.time(), tzinfo=zone)
        remaining = (candidate.astimezone(timezone.utc) - now).total_seconds()
        if 0 < remaining < (deadline - now).total_seconds():
            arrivals.append(remaining)
    if len(arrivals) < 3:
        raise ValueError("arrival_insufficient_remaining_history")
    return arrivals, "historical_last_return_clock"


def _prepare(base, now):
    """Build one low-confidence, empty-car demand scenario from cached recorder history."""
    from ha import run_async
    from car_charging_plan import _number, _read_entity_state

    entity = read_car_charging_config(base)["home_entity"]
    # The same authority validator must accept the standing policy, but still
    # reject actual EV availability. A retained fault cannot forecast a charge.
    if resolve_recurring_session(base, now=now).get("reason") != "ev_not_home":
        raise ValueError("arrival_policy_invalid")
    record = run_async(base.components.get_component("storage").load_checked("car_charging", "recurring_session"))
    if record is None or record["invalid"]:
        raise ValueError("arrival_policy_fault")
    home_transition = _read_entity_state(base, entity)["value_last_changed"].isoformat()
    cached = getattr(base, "car_charging_arrival_history", None)
    if not cached or cached["entity"] != entity or cached["home_transition"] != home_transition or not 0 <= (now - cached["at"]).total_seconds() < 3600:
        cached = {"entity": entity, "home_transition": home_transition, "at": now, "history": base.get_history_wrapper(entity, days=14, required=False)}
        base.car_charging_arrival_history = cached
    trips, left = _trips(cached["history"], now)
    latest_history_state = max((datetime.fromisoformat(item["last_changed"]) for item in cached["history"][0]), default=None)
    if latest_history_state is None or latest_history_state < datetime.fromisoformat(home_transition):
        base.car_charging_arrival_history = None
        raise ValueError("arrival_history_does_not_cover_departure")
    if left is None:
        raise ValueError("arrival_departure_not_observed")
    zone = ZoneInfo(base.args["timezone"])
    deadline = next_departure(base, now)
    remaining, cohort = _arrival_samples(trips, left, now, zone, deadline)
    expected = now + timedelta(seconds=_quantile(remaining, 0.75))
    low, high = (now + timedelta(seconds=_quantile(remaining, fraction)) for fraction in (0.1, 0.9))
    midnight = base.midnight_utc.astimezone(timezone.utc)
    start = math.ceil((max(expected, now + timedelta(minutes=PREDICT_STEP)) - midnight).total_seconds() / (60 * PREDICT_STEP)) * PREDICT_STEP
    capacity = _number(base.car_charging_battery_size[0], "arrival_capacity", 0.001)
    target = _number(base.car_charging_limit[0], "arrival_target", 0, capacity)
    rate = _number(base.car_charging_rate[0], "arrival_rate", 0.001)
    efficiency = _number(base.car_charging_loss, "arrival_efficiency", 0.001, 1)
    horizon = min((deadline - midnight).total_seconds() / 60, base.minutes_now + base.forecast_minutes)
    end = min(start + math.ceil(target / efficiency / rate * 60 / PREDICT_STEP) * PREDICT_STEP, math.floor(horizon / PREDICT_STEP) * PREDICT_STEP)
    energy = min(target / efficiency, rate * max(0, end - start) / 60)
    return {
        "status": "anticipated" if energy else "no_remaining_window",
        "prepared_at": now.isoformat(),
        "expected_arrival": expected.isoformat(),
        "arrival_p10": low.isoformat(),
        "arrival_p90": high.isoformat(),
        "departure_observed_at": left.isoformat(),
        "ready_by": deadline.isoformat(),
        "completed_trips": len(trips),
        "eligible_trips": len(remaining),
        "cohort": cohort,
        "history_fetched_at": cached["at"].isoformat(),
        "latest_history_state_at": latest_history_state.isoformat(),
        "confidence": "low",
        "soc_assumption_kwh": 0,
        "soc_assumption": "empty_car_demand_scenario_not_a_vehicle_reading",
        "soc_observed_at": None,
        "requested_target_kwh": target,
        "model_ac_kwh": energy,
        "shortfall_kwh": max(0, target - energy * efficiency),
        "guaranteed_target_met": False,
        "execution_permission": False,
        "requires_observed_home_and_connection": True,
        "_signature": _signature(base),
        "_plan_at": base.now_utc.isoformat(),
        "_home_transition": home_transition,
        "_slots": [{"start": start, "end": end, "kwh": energy, "octopus": False}] if energy else [],
    }


def prepare_arrival_forecast(base):
    """Refresh model-only demand once per native fetch; failure leaves household controls native."""
    from car_charging_plan import _observation_clock

    base.car_charging_arrival_forecast = None
    try:
        now = _observation_clock()
        if _eligible(base, now):
            base.car_charging_arrival_forecast = _prepare(base, now)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("arrival_") else "arrival_history_or_inputs_unusable"
        base.car_charging_arrival_forecast = {"status": "unavailable", "reason": reason, "execution_permission": False}


def model_arrival_slots(base):
    """Return detached model slots only while all original absence assumptions still hold."""
    from car_charging_plan import _observation_clock, _read_entity_state

    report = getattr(base, "car_charging_arrival_forecast", None)
    if not isinstance(report, dict) or report.get("status") != "anticipated":
        return []
    try:
        now = _observation_clock()
        if not 0 <= (now - datetime.fromisoformat(report["prepared_at"])).total_seconds() <= 300 or report["_plan_at"] != base.now_utc.isoformat() or report["_signature"] != _signature(base) or not _eligible(base, now):
            return []
        home = _read_entity_state(base, read_car_charging_config(base)["home_entity"])
        if home["value_last_changed"].isoformat() != report["_home_transition"]:
            return []
        return deepcopy(report["_slots"])
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return []


def arrival_evidence(base):
    """Expose assumptions in Show Debug without projecting them into an execution lease."""
    report = getattr(base, "car_charging_arrival_forecast", None)
    return {key: deepcopy(value) for key, value in report.items() if not key.startswith("_")} if isinstance(report, dict) else None
