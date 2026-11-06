"""Model-only arrival demand cannot become an EV command or a vehicle observation."""

from tests.car_planning_fixtures import car_settings

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from const import PREDICT_STEP
from car_charging_arrival import prepare_arrival_forecast, model_arrival_slots
from car_charging_plan import evaluate_car_charging
from prediction import Prediction
from tests.test_car_planning_actual import _set_entity
from tests.test_car_planning_recurring import recurring_inputs


def arrival_inputs(base):
    """Give the real native seams an absent car and three genuine historical round trips."""
    (fixture, store) = recurring_inputs(base)
    base.car_charging_plan_time = ["07:30:00"]
    base.car_charging_limit[0] = base.car_charging_battery_size[0] = 25.0
    base.car_charging_rate[0] = 6.77
    base.forecast_minutes = base.end_record = 1440
    base.car_charging_planned[0] = base.car_charging_now[0] = False
    base.car_charging_now_slots = [[] for _ in base.car_charging_slots]
    base.car_charging_arrival_forecast = None
    base.car_charging_arrival_history = None
    start = base.now_utc - timedelta(hours=1)
    history = []
    for days, hours in ((3, 2), (2, 3), (1, 4)):
        left = start - timedelta(days=days)
        for stamp, state in ((left - timedelta(hours=1), "home"), (left, "not_home"), (left + timedelta(minutes=30), "not_home"), (left + timedelta(hours=hours), "home")):
            history.append({"state": state, "last_changed": stamp.isoformat()})
    history.append({"state": "not_home", "last_changed": start.isoformat()})
    settings = car_settings(base)
    _set_entity(base, settings["home_entity"], "not_home", last_changed=start, last_updated=start)
    _set_entity(base, settings["connection_entity"], "disconnected", attributes={"retrievalstatus": "VALID"})
    return fixture, history


def test_arrival_forecast_is_model_only_and_no_soc_read(base):
    """An uncertain arrival adds native future demand, but neither an EV slot nor permission."""
    (_, writes, _, _), history = arrival_inputs(base)
    before = deepcopy(base.args)
    slots = deepcopy(base.car_charging_slots)
    getter = base.get_state_wrapper
    settings = car_settings(base)

    def guarded(*args, **kwargs):
        """Make even an indirect absent-vehicle SOC/meter read fail this acceptance test."""
        entity = kwargs.get("entity_id", args[0] if args else None)
        assert entity not in (settings["soc_entity"], "sensor.test_ev_energy", "sensor.test_ev_power")
        return getter(*args, **kwargs)

    with patch.object(base, "get_state_wrapper", side_effect=guarded), patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        base.fetch_sensor_data_car_planning()
        report = base.car_charging_arrival_forecast
        assert report["status"] == "anticipated", report
        assert report["completed_trips"] == 3 and report["eligible_trips"] == 3
        assert report["soc_assumption_kwh"] == 0 and report["soc_observed_at"] is None
        assert report["execution_permission"] is False and report["confidence"] == "low"
        model = base.car_charging_slots_model()
        assert model[0] and model[0][0]["start"] > base.minutes_now
        result = evaluate_car_charging(base, publish=False)
        assert result["status"] == "household_fallback" and not result["execution_enabled"]
        assert result["arrival_forecast"]["status"] == "anticipated"
        assert base.car_charging_pending is None
        base.publish_car_plan()
        published = base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]
        assert published["state"] == "off"
        assert not published["attributes"].get("valid_until")
    assert base.args == before and base.car_charging_slots == slots and not writes
    assert base.car_charging_soc[0] == 0


def test_arrival_predicted_energy_starts_after_expected_arrival(base):
    """Use the real Prediction model to prove hypothetical demand changes only future energy."""
    _, history = arrival_inputs(base)
    zeros = {minute: 0.0 for minute in range(0, base.forecast_minutes, PREDICT_STEP)}
    load = {minute: PREDICT_STEP / 60 for minute in zeros}
    with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        model = base.car_charging_slots_model()
        for active in (False, True):
            prediction = Prediction(base, zeros, zeros, load, load, car_charging_slots=model if active else base.car_charging_slots)
            prediction.run_prediction([], [], [], [], False, end_record=base.forecast_minutes, save="best")
            if active:
                assert abs(prediction.final_car_soc[0] - 25) < 0.01
                for stamp, value in prediction.predict_car_soc_time[0].items():
                    if datetime.fromisoformat(stamp) < base.midnight_utc + timedelta(minutes=model[0][0]["start"]):
                        assert value == 0, (stamp, value)
            else:
                assert prediction.final_car_soc[0] == 0
    assert base.car_charging_slots[0] == [] and base.car_charging_soc[0] == 0


def test_arrival_history_invalid_and_force_updates(base):
    """Unknown gaps, unpaired home entries and repeated home reports cannot invent trips."""
    for kind in ("empty", "unknown", "same", "future"):
        _, history = arrival_inputs(base)
        if kind == "empty":
            history = []
        elif kind == "unknown":
            for record in history:
                if record["state"] == "not_home":
                    record["state"] = "unavailable"
        elif kind == "same":
            history = [{**record, "state": "home"} for record in history]
        else:
            history.append({"state": "home", "last_changed": (base.now_utc + timedelta(hours=1)).isoformat()})
        with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
            prepare_arrival_forecast(base)
            assert not model_arrival_slots(base), (kind, base.car_charging_arrival_forecast)


def test_arrival_history_cache_and_actual_return(base):
    """Reuse recorder history, withdraw assumptions immediately on real arrival or permission loss."""
    _, history = arrival_inputs(base)
    with patch.object(base, "get_history_wrapper", return_value=[history]) as reader, patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        prepare_arrival_forecast(base)
        assert reader.call_count == 1
        assert reader.call_args.kwargs == {"days": 14, "required": False}
        assert model_arrival_slots(base)
        _set_entity(base, car_settings(base)["home_entity"], "home")
        assert not model_arrival_slots(base)
        prepare_arrival_forecast(base)
        assert not model_arrival_slots(base)
        assert reader.call_count == 1
    _, history = arrival_inputs(base)
    with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        _set_entity(base, car_settings(base)["permission_entity"], "off")
        assert not model_arrival_slots(base)


def test_arrival_late_shortfall_and_model_copy(base):
    """An uncertain late return cannot be labelled guaranteed completion or share command lists."""
    _, history = arrival_inputs(base)
    base.car_charging_plan_time = [(base.now_utc + timedelta(hours=4)).strftime("%H:%M:%S")]
    base.args["timezone"] = "Europe/London"
    with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        report = base.car_charging_arrival_forecast
        assert report["status"] == "anticipated" and report["shortfall_kwh"] > 0, report
        assert report["guaranteed_target_met"] is False
        slots = base.car_charging_slots_model()
        slots[0][0]["kwh"] = 999
        assert model_arrival_slots(base)[0]["kwh"] < 999 and not base.car_charging_slots[0]


def test_arrival_disabled_stale_and_manual_inputs(base):
    """Legacy, stale, malformed and manually controlled inputs retain their existing semantics."""
    for mode in ("off", "shadow", "manual", "relay_on", "stale", "native_slots"):
        _, history = arrival_inputs(base)
        with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
            prepare_arrival_forecast(base)
            if mode in ("off", "shadow"):
                car_settings(base)["mode"] = mode
            elif mode == "manual":
                base.car_charging_manual_soc[0] = True
            elif mode == "relay_on":
                _set_entity(base, car_settings(base)["relay_entity"], "on")
            elif mode == "stale":
                base.car_charging_arrival_forecast["prepared_at"] = (base.now_utc - timedelta(minutes=6)).isoformat()
            else:
                base.car_charging_slots[0] = [{"start": 0, "end": 10, "kwh": 1}]
            assert not model_arrival_slots(base), mode


def test_arrival_departure_change_rejects_old_trip_history(base):
    """A second departure between cycles cannot silently reuse the previous trip's origin."""
    _, history = arrival_inputs(base)
    with patch.object(base, "get_history_wrapper", return_value=[history]) as reader, patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        assert model_arrival_slots(base)
        _set_entity(base, car_settings(base)["home_entity"], "not_home", last_changed=base.now_utc, last_updated=base.now_utc)
        assert not model_arrival_slots(base)
        prepare_arrival_forecast(base)
        assert reader.call_count == 2
        assert base.car_charging_arrival_forecast["reason"] == "arrival_history_does_not_cover_departure"
        assert not model_arrival_slots(base)


def test_arrival_fault_and_deadline_change(base):
    """Retained physical faults and changed human intent invalidate the hypothetical demand."""
    _, history = arrival_inputs(base)
    with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        assert model_arrival_slots(base)
        base.car_charging_plan_time[0] = "08:30:00"
        assert not model_arrival_slots(base)
        prepare_arrival_forecast(base)
        assert base.car_charging_arrival_forecast["ready_by"].endswith("08:30:00+00:00")
        store = base.components.get_component("storage")
        store.records[("car_charging", "recurring_session")]["invalid"] = "recurring_meter_contradiction"
        prepare_arrival_forecast(base)
        assert base.car_charging_arrival_forecast["reason"] == "arrival_policy_fault"
        assert not model_arrival_slots(base)


def test_arrival_dst_uses_native_calendar_deadline(base):
    """Model slot offsets follow UTC duration, while the daily ready time stays local 07:30."""
    from zoneinfo import ZoneInfo

    for stamp in ("2026-03-28T19:00:00+01:00", "2026-10-24T19:00:00+02:00"):
        _, history = arrival_inputs(base)
        now = datetime.fromisoformat(stamp).astimezone(timezone.utc)
        shift = now - base.now_utc
        history = [{**item, "last_changed": (datetime.fromisoformat(item["last_changed"]) + shift).isoformat()} for item in history]
        base.now_utc = now
        base.midnight_utc = now.astimezone(ZoneInfo("Europe/Madrid")).replace(hour=0, minute=0, second=0, microsecond=0)
        base.minutes_now = int((now - base.midnight_utc).total_seconds() / 60)
        base.args["timezone"] = "Europe/Madrid"
        for name, state in (("home_entity", "not_home"), ("permission_entity", "on"), ("relay_entity", "off")):
            _set_entity(base, car_settings(base)[name], state, last_changed=now - timedelta(hours=1), last_updated=now - timedelta(hours=1))
        car_settings(base)["recurring_session"]["policy_authorised_at"] = (now - timedelta(days=1)).isoformat()
        with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=now):
            prepare_arrival_forecast(base)
            report = base.car_charging_arrival_forecast
            assert report["status"] == "anticipated", report
            deadline = datetime.fromisoformat(report["ready_by"])
            assert deadline.astimezone(ZoneInfo("Europe/Madrid")).strftime("%H:%M:%S") == "07:30:00"
            end = base.midnight_utc.astimezone(timezone.utc) + timedelta(minutes=model_arrival_slots(base)[0]["end"])
            assert end <= deadline


def test_arrival_long_trip_uses_observed_return_clock_fallback(base):
    """A long trip uses real daily return times instead of treating short trips as an ETA."""
    _, history = arrival_inputs(base)
    left = base.now_utc - timedelta(hours=10)
    history[-1]["last_changed"] = left.isoformat()
    _set_entity(base, car_settings(base)["home_entity"], "not_home", last_changed=left, last_updated=left)
    with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
        report = base.car_charging_arrival_forecast
        assert report["status"] == "anticipated" and report["cohort"] == "historical_last_return_clock", report
        assert report["eligible_trips"] == 3 and report["confidence"] == "low"
        assert datetime.fromisoformat(report["expected_arrival"]) > base.now_utc


def run_car_planning_arrival_tests(base):
    """Register independent model-only arrival acceptance at the native prediction boundary."""
    from tests.test_car_planning_template import shared_template_available, check_unavailable_template

    test_arrival_household_fallback_debug_escapes_hostile_fields(base)
    test_arrival_household_fallback_debug_handles_unavailable_forecast(base)
    if not shared_template_available():
        check_unavailable_template(base)
        return False
    for test in (
        test_arrival_forecast_is_model_only_and_no_soc_read,
        test_arrival_predicted_energy_starts_after_expected_arrival,
        test_arrival_history_invalid_and_force_updates,
        test_arrival_history_cache_and_actual_return,
        test_arrival_late_shortfall_and_model_copy,
        test_arrival_disabled_stale_and_manual_inputs,
        test_arrival_departure_change_rejects_old_trip_history,
        test_arrival_fault_and_deadline_change,
        test_arrival_dst_uses_native_calendar_deadline,
        test_arrival_long_trip_uses_observed_return_clock_fallback,
        test_arrival_household_fallback_debug_exposes_forecast,
    ):
        test(base)
    return False


def test_arrival_household_fallback_debug_exposes_forecast(base):
    """The hidden Show-Debug branch surfaces the low-confidence arrival forecast details."""
    from car_charging_view import render_car_charging_preview

    _, history = arrival_inputs(base)
    with patch.object(base, "get_history_wrapper", return_value=[history]), patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        prepare_arrival_forecast(base)
    report = {"status": "household_fallback", "execution_reason": "ev_unavailable", "arrival_forecast": base.car_charging_arrival_forecast}
    html = render_car_charging_preview(report, None)
    # Hidden debug span, never the visible mainline.
    assert html.startswith('<div class="car-charging-preview-debug" style="display:none;">')
    assert "EV authority withdrawn" in html
    # Required fields from the task: expected time, p10/p90 range, confidence,
    # sample size, source timestamp, explicit empty-car hypothetical kWh, shortfall.
    assert "Expected arrival:" in html
    assert "p10" in html and "p90" in html
    assert "Confidence:" in html
    assert "Sample size:" in html
    assert "Source timestamp:" in html
    assert "Hypothetical demand: empty-car scenario" in html
    assert "shortfall" in html
    # Never the table or visible mainline.
    assert "<table" not in html
    assert "<details" not in html
    assert "car-charging-preview-summary" not in html
    assert "car-charging-preview-banner" not in html


def test_arrival_household_fallback_debug_escapes_hostile_fields(base):
    """Hostile fields supplied by HA cannot break out of the hidden debug span."""
    from car_charging_view import render_car_charging_preview

    hostile = "<script>alert(1)</script>&\"'"
    forecast = {
        "status": "anticipated",
        "expected_arrival": (base.now_utc + timedelta(hours=1)).isoformat(),
        "arrival_p10": (base.now_utc + timedelta(minutes=45)).isoformat(),
        "arrival_p90": (base.now_utc + timedelta(hours=2)).isoformat(),
        "confidence": hostile,
        "completed_trips": 5,
        "eligible_trips": 3,
        "cohort": "all_remaining_trips " + hostile,
        "prepared_at": base.now_utc.isoformat(),
        "history_fetched_at": base.now_utc.isoformat(),
        "latest_history_state_at": base.now_utc.isoformat(),
        "departure_observed_at": (base.now_utc - timedelta(hours=1)).isoformat(),
        "ready_by": (base.now_utc + timedelta(hours=8)).isoformat(),
        "requested_target_kwh": 20.0,
        "model_ac_kwh": 15.0,
        "shortfall_kwh": 5.0,
        "guaranteed_target_met": False,
        "execution_permission": False,
        "soc_assumption_kwh": 0,
    }
    report = {"status": "household_fallback", "execution_reason": hostile, "arrival_forecast": forecast}
    html = render_car_charging_preview(report, None)
    # No literal script tag or stray quote reaches the live HTML.
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The forecast details still render (escaped) so Show Debug keeps working.
    assert "Expected arrival:" in html
    assert "Sample size:" in html
    assert "Hypothetical demand: empty-car scenario" in html


def test_arrival_household_fallback_debug_handles_unavailable_forecast(base):
    """An unavailable arrival forecast still surfaces its reason without HTML injection."""
    from car_charging_view import render_car_charging_preview

    forecast = {"status": "unavailable", "reason": "<bad> & 'quoted'", "prepared_at": base.now_utc.isoformat()}
    report = {"status": "household_fallback", "execution_reason": "ev_unavailable", "arrival_forecast": forecast}
    html = render_car_charging_preview(report, None)
    assert "Arrival forecast: unavailable" in html
    assert "Reason: " in html
    assert "<bad>" not in html
    assert "&lt;bad&gt;" in html
    # The unavailable branch never reveals the low-confidence fields that an
    # anticipated forecast would show.
    assert "Expected arrival:" not in html
    assert "Hypothetical demand: empty-car scenario" not in html
