"""Distinct economic, next-opportunity, immediate and solar-only EV contracts."""

from tests.car_planning_fixtures import car_settings

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from car_charging_solver import BatteryConfig, EVConfig, PlannerConfig, PlannerInputError, SiteConfig, Slot, plan
from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot, refresh_car_charging_lease
from car_charging_plan import _continuous_grid_support_shadow, _options, _replay, evaluate_car_charging
from tests.test_car_planning_actual import _actual_inputs, _set_entity


def preference_inputs(base, priority="asap", target=0.5):
    """Keep real template controls while isolating time preference from terminal credit."""
    inverter, writes, _, _ = _actual_inputs(base)
    car_settings(base)["ev_charging_priority"] = priority
    settings = car_settings(base)
    settings["session"]["departure"] = settings["confirmed_session"]["departure"] = (base.now_utc + timedelta(minutes=60)).isoformat()
    base.soc_kw = base.soc_max * 0.8
    inverter.soc_kw, inverter.soc_percent = base.soc_kw, 80
    base.car_charging_limit[0] = target
    base.metric_battery_value_scaling = 0
    base.metric_battery_cycle = 0
    return writes


def solar_forecast(base, start, end, conservative=True):
    """Provide enough PV for full EV power plus the complete household demand."""
    for minute in range(start, end, 5):
        base.pv_forecast_minute_step[minute] = 10 * 5 / 60
        if conservative:
            base.pv_forecast_minute10_step[minute] = 10 * 5 / 60


def solar_meters(base, pv=10000, load=1000, age=0):
    """Supply real-template local watt readings with genuine synthetic observation clocks."""
    for name, value in (("pv_power", pv), ("load_power", load)):
        entity = base.get_arg(name, indirect=False, index=0)
        _set_entity(base, entity, str(value), attributes={"unit_of_measurement": "W"}, last_updated=base.now_utc - timedelta(seconds=age))


def first_start(base, report):
    """Read the actual independently replayed EV start rather than a strategy label."""
    assert report["status"] == "candidate" and all(check["passed"] for check in report["native_checks"].values()), report
    return report["proposed_ev_windows"][0]["start"] - base.minutes_now


def test_four_distinct_preferences(base):
    """One tariff/PV snapshot yields NOW at 0, ASAP at 20, COST at 40 and SOLAR at 50."""
    writes = preference_inputs(base)
    base.rate_import.update({base.minutes_now + minute: 50 if minute < 20 else 10 if minute < 40 else -10 if minute < 45 else 20 for minute in range(60)})
    solar_forecast(base, 50, 60)
    starts = {}
    for priority in ("now", "asap", "cost", "solar"):
        car_settings(base)["ev_charging_priority"] = priority
        starts[priority] = first_start(base, evaluate_car_charging(base, publish=False))
    assert starts == {"now": 0, "asap": 20, "cost": 40, "solar": 50}, starts
    assert not writes


def test_asap_near_solar_before_cheaper_rate(base):
    """A conservative fifteen-minute solar opportunity within thirty minutes wins over a later discount."""
    preference_inputs(base)
    base.rate_import.update({base.minutes_now + minute: 50 if minute < 40 else 1 for minute in range(60)})
    solar_forecast(base, 15, 35)
    report = evaluate_car_charging(base, publish=False)
    assert first_start(base, report) == 15 and report["priority_selection_reason"] == "near_term_solar"
    # Nominal sunshine alone cannot justify waiting for that opportunity.
    base.pv_forecast_minute10_step = dict.fromkeys(base.pv_forecast_minute10_step, 0)
    report = evaluate_car_charging(base, publish=False)
    assert first_start(base, report) == 40 and report["priority_selection_reason"] == "next_cheaper_band"


def test_asap_deadline_and_flat_tariffs(base):
    """A cheap band too late to meet the target cannot delay charging; flat rates do not invent a wait."""
    preference_inputs(base, target=3)
    base.rate_import.update({base.minutes_now + minute: 50 if minute < 50 else 1 for minute in range(60)})
    report = evaluate_car_charging(base, publish=False)
    assert first_start(base, report) == 0 and report["priority_selection_reason"] == "deadline_or_no_opportunity"
    base.rate_import.update({base.minutes_now + minute: 10 for minute in range(60)})
    assert first_start(base, evaluate_car_charging(base, publish=False)) == 0


def test_now_can_use_shared_grid_support(base):
    """Immediate charging can choose supported Charge when a current Demand start fails the floor."""
    _, writes, _, _ = _actual_inputs(base)
    car_settings(base).update(ev_charging_priority="now", self_consumption_first=True)
    report = evaluate_car_charging(base, publish=False)
    assert first_start(base, report) == 0
    assert base.car_charging_pending["commands"][0]["target_soc"] > 0
    assert execute_car_charging_plan(base)[0] == "Charging", base.car_charging_report
    assert all(service in ("number/set_value", "select/select_option") for service, _ in writes)


def test_asap_current_discount_does_not_keep_deferring(base):
    """Once the next discount arrives, a still cheaper later rate cannot keep moving ASAP's start."""
    preference_inputs(base)
    base.rate_import[base.minutes_now - 1] = 50
    base.rate_import.update({base.minutes_now + minute: 10 if minute < 40 else 1 for minute in range(60)})
    report = evaluate_car_charging(base, publish=False)
    assert first_start(base, report) == 0 and report["priority_selection_reason"] == "next_cheaper_band"


def test_cost_compares_flexible_schedule(base):
    """Continuity may not hide a cheaper paired-safe split schedule under COST."""
    preference_inputs(base, priority="cost", target=0.45)
    base.car_charging_rate[0] = 3
    base.battery_rate_max_discharge = 0
    base.rate_import.update({base.minutes_now + minute: -5 if minute < 5 or 30 <= minute < 35 else 50 for minute in range(60)})
    chosen, attempts = _continuous_grid_support_shadow(base, _options(base))
    assert chosen and all(check["passed"] for check in chosen[3].values())
    assert [window["start"] - base.minutes_now for window in chosen[2][0]] == [0, 30]
    assert next(item["priority_reason"] for item in attempts if item.get("selected")) == "coupled_cost_flexible_schedule"


def test_solar_pure_supply_constraint():
    """Neither cheap grid nor a full battery can substitute for actual slot PV in solar-only search."""
    slots = [Slot(index, index * 5, 5, 0.1, pv, -50, 0) for index, pv in enumerate((0, 1))]
    ev = EVConfig(25, 0, 0.5, rate_kw=6, efficiency=1, deadline_min=10)
    battery = BatteryConfig(20, 15, 2, 5, 5, 1, 1, 2)
    config = PlannerConfig(beam_width=None, solar_ev_only=True)
    result = plan(slots, ev, battery, SiteConfig(10), config)
    assert [action.ev_action for action in result.actions] == ["off", "on"]
    assert result.actions[1].battery_mode == "demand" and result.actions[1].grid_import_kwh == 0
    assert not plan([replace(slot, pv_kwh=0) for slot in slots], ev, battery, SiteConfig(10), config).actions
    try:
        plan(slots, ev, battery, SiteConfig(10), replace(config, solar_ev_only=1))
    except PlannerInputError:
        pass
    else:
        raise AssertionError("Solar constraint accepted a non-boolean flag")


def test_solar_partial_and_no_sun(base):
    """An unmet solar target remains visible and separately authorised; zero sun never falls back to grid."""
    writes = preference_inputs(base, priority="solar", target=25)
    car_settings(base)["ev_partial_target_authorised"] = True
    solar_forecast(base, 15, 30)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "partial_candidate" and report["requested_ev_target_kwh"] == 25, report
    assert 0 < report["planned_ev_kwh_by_departure"] < 25 and report["ev_target_shortfall_kwh"] > 0
    assert all(15 <= window["start"] - base.minutes_now < 30 for window in report["proposed_ev_windows"])
    assert all(not command["ev_on"] or command["target_soc"] == 0 for command in base.car_charging_pending["commands"])
    base.pv_forecast_minute_step = dict.fromkeys(base.pv_forecast_minute_step, 0)
    base.pv_forecast_minute10_step = dict.fromkeys(base.pv_forecast_minute10_step, 0)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "no_plan_found" and base.car_charging_pending is None
    assert report["ev_target_shortfall_kwh"] == 25 and report["priority_selection_reason"] == "solar_surplus_not_validated"
    assert not writes


def test_solar_native_replay_rejects_forged_supply(base):
    """The independent native replay rejects an EV-ON schedule replayed without its forecast solar supply."""
    preference_inputs(base, priority="solar")
    solar_forecast(base, 0, 60)
    options = _options(base)
    candidate, _ = _continuous_grid_support_shadow(base, options)
    assert candidate
    result, slots, windows, _ = candidate
    base.pv_forecast_minute10_step = dict.fromkeys(base.pv_forecast_minute10_step, 0)
    check = _replay(base, options, result, slots, windows, 1)
    assert not check["passed"] and "solar_surplus_forecast_insufficient" in check["reasons"]


def test_solar_live_gate_and_lease_withdrawal(base):
    """Fresh surplus permits a short lease; fading PV withdraws it without any direct relay write."""
    writes = preference_inputs(base, priority="solar", target=3)
    solar_forecast(base, 0, 60)
    solar_meters(base)
    assert first_start(base, evaluate_car_charging(base, publish=False)) == 0
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    receipt = base.car_charging_execution
    assert receipt and (datetime.fromisoformat(receipt["expires_at"]) - base.now_utc).total_seconds() <= 60
    count = len(writes)
    later = base.now_utc + timedelta(seconds=15)
    _set_entity(base, car_settings(base)["relay_entity"], "on", last_changed=base.now_utc)
    _set_entity(base, "sensor.test_ev_power", "6.77", attributes={"unit_of_measurement": "kW"}, last_updated=later)
    _set_entity(base, "sensor.test_ev_energy", "100.01", attributes={"unit_of_measurement": "kWh"}, last_updated=later)
    solar_meters(base, load=7770)
    with patch("car_charging_plan._observation_clock", return_value=later):
        refresh_car_charging_lease(base)
        assert base.car_charging_execution is not None, base.car_charging_report
        solar_meters(base, pv=3000, load=7770)
        refresh_car_charging_lease(base)
    assert base.car_charging_execution is None and len(writes) == count
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
    assert base.car_charging_report["execution_reason"] == "solar_surplus_insufficient"


def test_solar_lease_survives_calculation_but_not_stale_power(base):
    """A normal calculation gap may renew from fresh readings, never from minute-old evidence."""
    preference_inputs(base, priority="solar", target=3)
    solar_forecast(base, 0, 60)
    solar_meters(base)
    evaluate_car_charging(base, publish=False)
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    later = base.now_utc + timedelta(seconds=40)
    _set_entity(base, car_settings(base)["relay_entity"], "on", last_changed=base.now_utc)
    for entity, value, unit in (
        (base.get_arg("pv_power", indirect=False, index=0), "10000", "W"),
        (base.get_arg("load_power", indirect=False, index=0), "7770", "W"),
        ("sensor.test_ev_power", "6.77", "kW"),
        ("sensor.test_ev_energy", "100.01", "kWh"),
    ):
        _set_entity(base, entity, value, attributes={"unit_of_measurement": unit}, last_updated=later)
    with patch("car_charging_plan._observation_clock", return_value=later):
        refresh_car_charging_lease(base)
        receipt = base.car_charging_execution
        assert receipt and datetime.fromisoformat(receipt["expires_at"]) <= later + timedelta(seconds=60), base.car_charging_report
    with patch("car_charging_plan._observation_clock", return_value=later + timedelta(seconds=61)):
        refresh_car_charging_lease(base)
    assert base.car_charging_execution is None
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_solar_stale_inputs_and_mapping_change(base):
    """Forecasts cannot grant solar authority with stale local readings, or after remapping their source."""
    writes = preference_inputs(base, priority="solar")
    solar_forecast(base, 0, 60)
    solar_meters(base, age=61)
    evaluate_car_charging(base, publish=False)
    assert execute_car_charging_plan(base) == ("EV blocked", "solar_power_stale_or_future") and not writes
    # The normal update cycle clears its previous error before fetching fresh evidence.
    base.had_errors = False
    solar_meters(base)
    evaluate_car_charging(base, publish=False)
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    count = len(writes)
    base.args["pv_power"] = ["sensor.different_pv"]
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count


def test_solar_absence_before_meter_reads(base):
    """An away vehicle requires neither EV SOC nor local solar meters to hand back household control."""
    from tests.test_car_planning_session import session_inputs

    _, writes, _, _ = session_inputs(base)
    settings = car_settings(base)
    settings["ev_charging_priority"] = "solar"
    _set_entity(base, settings["home_entity"], "not_home")
    original = base.get_state_wrapper
    forbidden = {settings["soc_entity"], base.get_arg("pv_power", indirect=False, index=0), base.get_arg("load_power", indirect=False, index=0)}

    def guarded(*args, **kwargs):
        """Reject evidence reads whose availability precondition has failed."""
        assert kwargs.get("entity_id", args[0] if args else None) not in forbidden
        return original(*args, **kwargs)

    with patch.object(base, "get_state_wrapper", side_effect=guarded):
        assert evaluate_car_charging(base, publish=False)["status"] == "household_fallback"
    assert not writes


def run_car_planning_preferences_tests(base, template_available):
    """Run the pure contract independently and exercise full integrations only with the real template."""
    test_solar_pure_supply_constraint()
    if template_available:
        from unit_test import create_predbat

        for test in (
            test_four_distinct_preferences,
            test_asap_near_solar_before_cheaper_rate,
            test_asap_deadline_and_flat_tariffs,
            test_asap_current_discount_does_not_keep_deferring,
            test_now_can_use_shared_grid_support,
            test_cost_compares_flexible_schedule,
            test_solar_partial_and_no_sun,
            test_solar_native_replay_rejects_forged_supply,
            test_solar_live_gate_and_lease_withdrawal,
            test_solar_lease_survives_calculation_but_not_stale_power,
            test_solar_stale_inputs_and_mapping_change,
            test_solar_absence_before_meter_reads,
        ):
            fresh = create_predbat()
            with patch("car_charging_plan._observation_clock", side_effect=lambda: fresh.now_utc):
                test(fresh)
