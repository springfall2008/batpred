"""Opt-in grid-charge sharing models actual flows without adding execution authority."""

from tests.car_planning_fixtures import car_settings, configure_car_settings

from dataclasses import replace
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace

from car_charging_solver import BatteryConfig, EVConfig, SiteConfig, PlannerConfig, Slot, _segment, plan
from car_charging_plan import _options, _continuous_grid_support_shadow, _native_windows, _replay, evaluate_car_charging
from prediction import Prediction
from tests.test_car_planning_native import _setup, _controls, _set_entity
from tests.test_infra import reset_rates


def test_grid_support_recorded_charge_request():
    """A synthetic 7 kW load needs battery support, not an unconstrained grid purchase."""
    battery = BatteryConfig(40, 16, 4, 3 / 0.96, 8 * 0.96, 0.95 * 0.96, 0.95 * 0.96, 4)
    ev = EVConfig(40, 4, 35, rate_kw=7, efficiency=0.9)
    site = SiteConfig(6, 0, 16, charge_import_cap_kw=6)
    flows, end = _segment(5, 1, 6, 0, battery.initial_kwh, "charge", battery, ev, site)
    assert len(flows) == 1 and abs(flows[0].grid_import_kw - 6) < 1e-10
    assert flows[0].battery_charge_kw == 0 and abs(flows[0].battery_discharge_kw - 1) < 1e-10
    assert end < battery.initial_kwh and abs(end - (battery.initial_kwh - 1 / 12 / battery.discharge_efficiency)) < 1e-10
    assert _segment(5, 1, 6, 0, battery.initial_kwh, "charge", battery, ev, SiteConfig(6, 0, 16)) is None
    for mode in ("demand", "hold"):
        assert _segment(5, 1, 0, 0, battery.initial_kwh, mode, battery, ev, site) == _segment(5, 1, 0, 0, battery.initial_kwh, mode, battery, ev, SiteConfig(6, 0, 16))


def test_grid_support_headroom_and_exhaustion():
    """Charging yields first; insufficient energy/rate never becomes a fake capped import."""
    battery = BatteryConfig(10, 5, 1, 3, 8, 0.9, 0.9, 1)
    ev = EVConfig(40, 0, 35, rate_kw=7, efficiency=0.9, pause_battery_percent=0)
    site = SiteConfig(6, 0, 16, charge_import_cap_kw=6)
    for load, pv, expected_charge, expected_discharge in ((4.5, 0, 1.5, 0), (7.5, 1, 0, 0.5), (2, 4, 3, 0)):
        flows, _ = _segment(5, load, 0, pv, 5, "charge", battery, ev, site)
        for flow in flows:
            assert abs(flow.battery_charge_kw - expected_charge) < 1e-10 and abs(flow.battery_discharge_kw - expected_discharge) < 1e-10
            assert abs(flow.grid_import_kw + pv + flow.battery_discharge_kw - load - flow.battery_charge_kw - flow.curtailed_kw) < 1e-10
    assert _segment(5, 7.5, 0, 0, 5, "charge", replace(battery, max_discharge_kw=0.5), ev, site) is None
    assert _segment(5, 7.5, 0, 0, 1.01, "charge", battery, ev, site) is None
    flows, end = _segment(5, 4.5, 0, 0, 9.99, "charge", battery, ev, site)
    assert len(flows) == 2 and abs(end - 10) < 1e-10 and all(flow.grid_import_kw <= 6 for flow in flows)
    for value in (True, float("nan"), -1, 7):
        try:
            plan([Slot(0, 0, 5, 0.1, 0, 10, 0)], ev, battery, replace(site, charge_import_cap_kw=value), PlannerConfig())
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid support cap accepted")


def _support_fixture(base):
    """Explicit shadow setting and cached entities authorise modelling, never control."""
    _setup(base, minutes=60)
    base.set_read_only = base.set_discharge_during_charge = True
    car_settings(base).update(control_model="deye_solarman", grid_charge_support={"enabled_entity": "switch.test_grid_support", "limit_entity": "number.test_grid_support"})
    _set_entity(base, "switch.test_grid_support", "on")
    _set_entity(base, "number.test_grid_support", "6000")


def test_grid_support_continuous_ev_before_solar():
    """Grid and battery can supply an uninterrupted fixed-rate EV block before departure."""
    battery = BatteryConfig(32.154, 12.21852, 3.2154, 3 / 0.96, 8.192 * 0.96, 0.97 * 0.96, 0.97 * 0.96, 3.2154)
    ev = EVConfig(25, 3.25, 3.25 + 6.77 * 0.92, rate_kw=6.77, efficiency=0.92, deadline_min=60, initial_off_minutes=10)
    slots = [Slot(index, index * 5, 5, 0.5 / 12, 0 if index < 12 else 1, 10 if index < 12 else 0, 0) for index in range(24)]
    result = plan(slots, ev, battery, SiteConfig(6, 0, 16, charge_import_cap_kw=6), PlannerConfig(allowed_battery_modes=("charge",)))
    assert result.status == "feasible" and result.diagnostics.ev_stop_target_met
    assert all(action.ev_action == "on" and abs(action.ev_on_minutes - 5) < 1e-8 for action in result.actions[:12])
    assert all(action.ev_action == "off" for action in result.actions[12:])
    assert abs(sum(action.ev_delivered_kwh for action in result.actions) - 6.77) < 1e-8
    assert all(abs(segment.ev_kw - 6.77) < 1e-8 and abs(segment.battery_discharge_kw - 1.27) < 1e-8 for action in result.actions[:12] for segment in action.segments)
    # A higher target cannot be rescued by the cheap sunshine AFTER departure.
    missed = plan(slots, replace(ev, stop_soc_kwh=25), battery, SiteConfig(6, 0, 16, charge_import_cap_kw=6), PlannerConfig())
    assert missed.status == "bounded_no_plan" and not missed.actions


def test_grid_support_options_fail_closed(base):
    """Wrong models, missing apply observations or unavailable caps fail closed."""
    _support_fixture(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        assert _options(base)["grid_charge_support_w"] == 6000
        for change in ({"control_model": "native"}, {"grid_charge_support": True}):
            original = deepcopy(car_settings(base))
            car_settings(base).update(change)
            try:
                _options(base)
            except ValueError:
                pass
            else:
                raise AssertionError("Grid sharing accepted outside its contract")
            configure_car_settings(base, original)
        car_settings(base)["mode"] = "apply"
        assert "measured_ev_soc_required" in _options(base)["entity_blockers"]
        car_settings(base)["mode"] = "shadow"
        for entity, value in (("switch.test_grid_support", "off"), ("number.test_grid_support", "unavailable"), ("number.test_grid_support", "nan"), ("number.test_grid_support", "6500")):
            _support_fixture(base)
            _set_entity(base, entity, value)
            assert "grid_charge_support_unavailable" in _options(base)["entity_blockers"]
    _support_fixture(base)
    before = _controls(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc), patch("inverter_service_control.validate_template"):
        report = evaluate_car_charging(base, publish=False)
    assert report["grid_charge_support_w"] == 6000 and not report["execution_enabled"]
    assert _controls(base) == before and not getattr(base, "plan_grid_charge_support_w", 0)
    assert getattr(base, "car_charging_pending", None) is None


def test_grid_support_continuous_candidate_is_paired(base):
    """Continuity changes real reservations, keeps both gates, and falls back on replay failure."""
    _support_fixture(base)
    before = _controls(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
    chosen, attempts = _continuous_grid_support_shadow(base, options)
    assert chosen is not None and 1 <= len(attempts) <= 12
    result, slots, windows, checks = chosen
    assert all(check["passed"] for check in checks.values())
    assert all(a["end"] == b["start"] for a, b in zip(windows[0], windows[0][1:]))
    assert all(abs(window["kwh"] - 6.77 / 12) < 1e-8 for window in windows[0])
    assert _controls(base) == before
    for change in ({"grid_charge_support_w": 0}, {"house_only": True}):
        assert _continuous_grid_support_shadow(base, {**options, **change}) == (None, [])
    for change in ({"mode": "apply"}, {"initially_on": True, "initial_off_minutes": 0, "deadline": 30}):
        continued, runs = _continuous_grid_support_shadow(base, {**options, **change})
        assert continued is not None and all(check["passed"] for check in continued[3].values())
        if change.get("initially_on"):
            assert len(runs) == 1 and runs[0]["start_offset"] == 0
    assert _controls(base) == before
    with patch("car_charging_plan._replay_candidate", return_value=(windows, {"nominal": {"passed": False}, "pv10": {"passed": True}})):
        rejected, attempts = _continuous_grid_support_shadow(base, options)
    assert rejected is None and all(not attempt["native_passed"] for attempt in attempts)


def test_grid_support_replay_rejects_hidden_reserve_tail(base):
    """The independent bridge uses exact intra-step peaks even without companion import diagnostics."""
    _support_fixture(base)
    base.forecast_minutes = base.end_record = 5
    base.soc_kw = base.reserve + 0.1
    base.car_charging_limit = [0] * len(base.car_charging_limit)
    base.load_minutes_step = base.load_minutes_step10 = {0: 6.5 / 12}
    base.pv_forecast_minute_step = base.pv_forecast_minute10_step = {0: 0}
    options = dict(
        mode="shadow", control_model="deye_solarman", grid_charge_support_w=5000, ev_soc=0, import_limit_w=6000, terminal_min=base.reserve, initially_on=False, initial_off_minutes=10, pause=15, restart=25, arrival=0, deadline=5, off_minutes=10, load_cap=16
    )
    result = SimpleNamespace(actions=[SimpleNamespace(slot_index=0, battery_mode="charge", ev_action="off")])
    slots = [Slot(0, 0, 5, 6.5 / 12, 0, 10, 0)]
    original = Prediction.run_prediction

    def without_diagnostic(pred, *args, **kwargs):
        """Simulate independent installation of the combined PR without the diagnostic PR."""
        returned = original(pred, *args, **kwargs)
        pred.import_limit_diagnostics = None
        return returned

    with patch.object(Prediction, "run_prediction", without_diagnostic):
        check = _replay(base, options, result, slots, _native_windows(base, result, slots), 0)
    assert not check["passed"] and "site_import_exceeded" in check["reasons"]
    assert abs(check["peak_import_w"] - 6500) < 1e-6 and check["checked_minutes"] == 5


def test_grid_support_domestic_targets_follow_solar(base):
    """Future PV changes domestic charging, but cannot replace the unchanged earlier EV deadline."""
    plans = []
    for sunny in (False, True):
        _support_fixture(base)
        base.forecast_minutes = base.end_record = 360
        reset_rates(base, 10, 0)
        base.rate_import.update({base.minutes_now + minute: 40 for minute in range(180, 360)})
        base.soc_kw = base.soc_max * 0.40
        # Hold the same terminal floor without rewarding extra end-of-horizon stock.
        base.metric_battery_value_scaling = 0
        base.load_minutes_step = base.load_minutes_step10 = {minute: 2 / 12 for minute in range(0, 360, 5)}
        base.pv_forecast_minute_step = base.pv_forecast_minute10_step = {minute: 4 / 12 if sunny and minute >= 180 else 0 for minute in range(0, 360, 5)}
        with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
            options = _options(base)
        chosen, _ = _continuous_grid_support_shadow(base, options)
        assert chosen is not None
        result, slots, windows, checks = chosen
        assert all(check["passed"] and check["final_ev_kwh"] >= 3 for check in checks.values())
        assert all(window["end"] <= base.minutes_now + 60 for window in windows[0])
        assert all(limit < base.soc_max for limit in windows[2])
        plans.append(sum(action.battery_charge_kwh for action in result.actions if action.pv_kwh == 0))
    assert plans[1] < plans[0]


def test_initial_demand_is_a_scoped_search_constraint():
    """Consistent demand-first actions can still use later grid preparation for the same floor."""
    slots = [Slot(index, index * 5, 5, 0, 0, 10, 0) for index in range(3)]
    battery = BatteryConfig(10, 2, 1, 3, 8, 1, 1, 2.4, terminal_value_per_kwh=100)
    ev = EVConfig(40, 0, 0, rate_kw=7, efficiency=0.9, deadline_min=15)
    site = SiteConfig(6, 0, 16)
    config = PlannerConfig(beam_width=None, allowed_battery_modes=("demand", "charge"))
    default = plan(slots, ev, battery, site, config)
    preferred = plan(slots, ev, battery, site, replace(config, initial_battery_mode="demand"))
    assert default.actions[0].battery_mode == "charge"
    assert preferred.status == "feasible" and preferred.actions[0].battery_mode == "demand"
    assert preferred.actions[0].grid_import_kwh == 0 and preferred.actions[0].battery_soc_kwh_end == 2
    assert any(action.battery_mode == "charge" for action in preferred.actions[1:])
    assert preferred.diagnostics.final_battery_soc_kwh >= 2.4
    for mode in (True, "unsupported", "hold"):
        try:
            plan(slots, ev, battery, site, replace(config, initial_battery_mode=mode))
        except ValueError:
            pass
        else:
            raise AssertionError("Unsupported initial mode accepted")


def test_self_consumption_preference_is_paired_and_explicit(base):
    """Opt-in sources preference changes the search, not source settings or replay guards."""
    _support_fixture(base)
    car_settings(base).update(self_consumption_first=True, ev_charging_priority="asap")
    before = deepcopy(base.args)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
    chosen, attempts = _continuous_grid_support_shadow(base, options)
    assert chosen is not None and chosen[0].actions[0].battery_mode == "demand"
    assert attempts[0]["initial_demand_only"] is True and attempts[0]["native_passed"]
    assert all(check["passed"] for check in chosen[3].values())
    assert base.args == before and not getattr(base, "car_charging_pending", None)
    # Force the initial-demand trial to fail independent replay. The fallback
    # must be separately checked, rather than hiding the rejection or dropping a gate.
    from car_charging_plan import _replay_candidate as original

    def reject_demand(base, trial, result, slots):
        """Inject a paired-check failure only in the preferred trial."""
        windows, checks = original(base, trial, result, slots)
        if trial.get("initial_demand_only"):
            checks["pv10"]["passed"] = False
        return windows, checks

    with patch("car_charging_plan._replay_candidate", side_effect=reject_demand):
        fallback, attempts = _continuous_grid_support_shadow(base, options)
    assert fallback is not None and all(check["passed"] for check in fallback[3].values())
    assert attempts[0]["initial_demand_only"] and not attempts[0]["native_passed"]
    assert not attempts[1]["initial_demand_only"] and attempts[1]["native_passed"]
    assert base.args == before
    for value in ("yes", 1, None):
        car_settings(base)["self_consumption_first"] = value
        with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
            try:
                _options(base)
            except ValueError:
                pass
            else:
                raise AssertionError("Non-boolean source preference accepted")


def run_car_planning_grid_support_tests(base):
    """Run deterministic dispatch and authority-boundary regressions."""
    for test in (test_grid_support_recorded_charge_request, test_grid_support_headroom_and_exhaustion, test_grid_support_continuous_ev_before_solar):
        test()
    test_grid_support_options_fail_closed(base)
    test_grid_support_continuous_candidate_is_paired(base)
    test_grid_support_replay_rejects_hidden_reserve_tail(base)
    test_grid_support_domestic_targets_follow_solar(base)
    test_initial_demand_is_a_scoped_search_constraint()
    test_self_consumption_preference_is_paired_and_explicit(base)
    return False
