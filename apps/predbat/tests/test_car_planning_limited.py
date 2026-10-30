"""Battery power sharing, future-floor reachability and native replay regressions."""

from copy import copy, deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from const import PREDBAT_MAX_CARS
from car_charging_solver import BatteryConfig, EVConfig, PlannerConfig, SiteConfig, Slot, plan
from car_charging_plan import _native_windows, _options, _replay, build_candidate
from car_charging_replay import discharge_limit_w, replay_discharge_limits
from tests.test_car_planning_native import _setup


def test_fixed_ev_can_share_grid_and_battery(base):
    """A discharge ceiling permits sharing without modulating or starving the EV."""
    slots = [Slot(i, i * 15, 15, 0.25, 0, 10, 0) for i in range(8)]
    battery = BatteryConfig(20, 7, 2, 3, 8, 1, 1, 2)
    ev = EVConfig(25, 0, 8, 6.77, 1, 120)
    site = SiteConfig(6, 0, 12)
    full_only = plan(slots, ev, battery, site, PlannerConfig(time_step_minutes=15))
    shared = plan(slots, ev, battery, site, PlannerConfig(time_step_minutes=15, allow_limited_discharge=True))
    assert full_only.status == "bounded_no_plan"
    assert shared.status == "feasible" and shared.diagnostics.ev_stop_target_met
    assert any(action.discharge_limit_kw is not None for action in shared.actions)
    for action in shared.actions:
        for segment in action.segments:
            assert segment.ev_kw in (0, 6.77)
            assert segment.grid_import_kw <= 6 + 1e-9
            assert abs(segment.pv_kw + segment.grid_import_kw + segment.battery_discharge_kw - segment.load_kw - segment.ev_kw - segment.battery_charge_kw - segment.grid_export_kw - segment.curtailed_kw) < 1e-8
            if action.discharge_limit_kw is not None:
                assert segment.battery_discharge_kw <= action.discharge_limit_kw + 1e-9


def test_future_manual_floor_survives_a_narrow_beam(base):
    """Reachability keeps required preparation without imposing the future floor early."""
    slots = [Slot(i, i * 30, 30, 0, 0, 10, 0, 4 if i == 3 else 0) for i in range(4)]
    battery = BatteryConfig(10, 2.5, 1, 1, 2, 1, 1, 1)
    ev = EVConfig(40, 0, 0, rate_kw=7, efficiency=0.9, deadline_min=120)
    result = plan(slots, ev, battery, SiteConfig(2, 0), PlannerConfig(time_step_minutes=30, beam_width=1))
    assert result.status == "feasible"
    assert result.battery_soc_trace[0] == 2.5 < 4
    assert result.battery_soc_trace[3] >= 4
    assert [action.battery_mode for action in result.actions[:3]] == ["charge"] * 3


def test_segmented_native_matches_constant_rate_replay(base):
    """Whole-horizon and stepped native predictions agree within documented rounding."""
    _setup(base)
    options = _options(base)
    result, slots = build_candidate(base, options)
    assert result.actions
    windows = _native_windows(base, result, slots)
    whole = _replay(base, options, result, slots, windows, 0)
    model = copy(base)
    model.prediction_kernel_enable = False
    model.metric_standing_charge = 0
    model.car_charging_soc = [options["ev_soc"]] * PREDBAT_MAX_CARS
    model.car_charging_limit_model = [options["ev_soc"] + sum(window["kwh"] for window in windows[0]) * base.car_charging_loss + 1] * PREDBAT_MAX_CARS
    model.car_charging_slots = [windows[0]] + [[] for _ in range(PREDBAT_MAX_CARS - 1)]
    before = deepcopy((base.car_charging_slots, base.battery_rate_max_discharge, base.set_charge_window))
    stepped = replay_discharge_limits(model, result, slots, 0)
    assert abs(stepped.final_soc - whole["final_battery_kwh"]) <= stepped.rounding_bound_kwh + 0.001
    assert abs(stepped.final_metric - base.cost_today_sofar - whole["horizon_energy_cost"]) < 0.01
    assert abs(stepped.import_limit_diagnostics["peak_w"] - whole["peak_import_w"]) <= 1
    assert stepped.import_limit_diagnostics["checked_minutes"] == base.forecast_minutes
    assert before == (base.car_charging_slots, base.battery_rate_max_discharge, base.set_charge_window)


def test_limited_rate_preserves_original_native_curve_ceiling(base):
    """Cap the current-rate input, not the maximum used to derive SOC/temperature curves."""
    _setup(base, minutes=5)
    base.battery_discharge_power_curve = {percent: 0.5 for percent in range(101)}
    base.car_charging_soc = [0.0] * PREDBAT_MAX_CARS
    base.car_charging_limit_model = [25.0] * PREDBAT_MAX_CARS
    base.car_charging_slots = [[{"start": base.minutes_now, "end": base.minutes_now + 5, "kwh": 6.77 / 12, "average": 10}]] + [[] for _ in range(PREDBAT_MAX_CARS - 1)]
    base.prediction_kernel_enable = False
    action = SimpleNamespace(battery_mode="limited_demand", discharge_limit_kw=1.772)
    result = SimpleNamespace(actions=[action])
    slot = Slot(0, 0, 5, 1 / 12, 0, 10, 0)
    replay = replay_discharge_limits(base, result, [slot], 0)
    assert replay.import_limit_diagnostics["peak_w"] <= 6000
    assert replay.final_car_soc[0] > 0
    assert 0 < discharge_limit_w(base, action) <= base.battery_rate_max_discharge * 60000


def run_car_planning_limited_tests(unused_base):
    """Run each bounded regression with a fresh fixture and no network authority."""
    from unit_test import create_predbat

    for test in (test_fixed_ev_can_share_grid_and_battery, test_future_manual_floor_survives_a_narrow_beam, test_segmented_native_matches_constant_rate_replay, test_limited_rate_preserves_original_native_curve_ceiling):
        base = create_predbat()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in combined tests")):
            test(base)
        print("PASS:", test.__name__)
    return False
