"""Generic integration tests joining thermal schedules to the real battery planner."""

from copy import deepcopy
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from math import isclose
from itertools import chain, repeat
from unittest.mock import patch

from prediction import Prediction
from predheat_model import HeatingConfig, HeatingInput, HeatingState
from tests.test_infra import reset_inverter, reset_rates

VENUE_FIELDS = (
    "load_minutes_step",
    "load_minutes_step10",
    "load_minutes_step90",
    "charge_window_best",
    "charge_limit_best",
    "export_window_best",
    "export_limits_best",
    "rate_import",
    "rate_export",
    "low_rates",
    "high_export_rates",
    "charge_window",
    "charge_limit",
    "export_window",
    "export_limits",
    "rate_min_forward",
    "rate_export_max_forward",
    "pv_forecast_minute_step",
    "soc_kw",
    "soc_max",
    "minutes_now",
    "forecast_minutes",
)


def _engine(base, pv=False, export_price=5.0):
    """Prepare deterministic five-minute forecasts through the existing engine fixture."""
    reset_inverter(base)
    base.minutes_now = 0
    base.midnight_utc = datetime(2026, 11, 6, tzinfo=timezone.utc)
    base.now_utc = base.midnight_utc
    base.now_utc_real = base.now_utc
    base.forecast_minutes = 24 * 60
    base.soc_max = 5.0
    base.soc_kw = 1.0
    base.reserve = 0.5
    base.reserve_percent = 10.0
    base.best_soc_min = 0.5
    base.best_soc_max = 5.0
    base.battery_rate_max_charge = 3.0 / 60
    base.battery_rate_max_charge_dc = 3.0 / 60
    base.battery_rate_max_discharge = 3.0 / 60
    base.inverter_limit = 6.0 / 60
    base.export_limit = 6.0 / 60
    base.metric_battery_cycle = 0.0
    base.metric_self_sufficiency = 0.0
    base.metric_battery_value_scaling = 1.0
    base.metric_battery_value_export_scaling = 1.0
    base.iboost_value_scaling = 0.0
    base.pv_metric10_weight = 0.0
    base.pv_metric90_weight = 0.0
    base.debug_enable = False
    base.prediction_kernel_enable = False
    base.calculate_best_charge = True
    base.calculate_best_export = True
    base.calculate_second_pass = False
    base.calculate_export_first = False
    base.rate_import = {}
    base.rate_export = {}
    reset_rates(base, 30.0, export_price)
    for minute in range(30, 330):
        base.rate_import[minute] = 8.0
    base.rate_scan(base.rate_import, print=False)
    base.rate_min_base = 8.0
    base.rate_max_base = 30.0
    base.rate_export_min_base = export_price
    base.rate_min_forward = {minute: 8.0 if minute < 330 else 30.0 for minute in range(2881)}
    base.rate_export_max_forward = {minute: export_price for minute in range(2881)}
    base.charge_window_best = [{"start": 30, "end": 330, "average": 8.0}]
    base.charge_limit_best = [0.0]
    base.low_rates = deepcopy(base.charge_window_best)
    base.high_export_rates = []
    base.charge_window = deepcopy(base.charge_window_best)
    base.charge_limit = [0.0]
    base.export_window_best = []
    base.export_limits_best = []
    base.load_minutes_step = {minute: 0.1 / 12 for minute in range(0, 1440, 5)}
    base.load_minutes_step10 = dict(base.load_minutes_step)
    base.load_minutes_step90 = dict(base.load_minutes_step)
    base.pv_forecast_minute_step = {minute: (2.0 / 12 if pv and 660 <= minute < 900 else 0.0) for minute in range(0, 1440, 5)}
    base.pv_forecast_minute10_step = dict(base.pv_forecast_minute_step)
    base.pv_forecast_minute90_step = dict(base.pv_forecast_minute_step)
    base.prediction = Prediction(base, base.pv_forecast_minute_step, base.pv_forecast_minute10_step, base.load_minutes_step, base.load_minutes_step10)
    return base


def _thermal():
    """Use the public pure model with an explicit radiator correction and thermal store."""
    config = HeatingConfig(
        heat_max_power=6000,
        heat_min_power=1000,
        heat_cop=3,
        heat_output=5000,
        heat_volume=100,
        heat_loss_watts=120,
        watt_per_degree=3000,
        hysteresis_on=0.1,
        hysteresis_off=0.1,
        delta_correction=tuple((degree, degree / 50) for degree in range(76)),
    )
    return HeatingInput(config, HeatingState(20.2, 35, False), (20.2,) * 288, (6.0,) * 288)


def _request(thermal=None):
    """Make night setback and a 06:00 comfort deadline explicit in the request."""
    from predheat_joint import JointHeatingRequest

    return JointHeatingRequest(
        thermal_input=thermal or _thermal(),
        minimum_temperature_c=tuple(18.0 if index < 72 else 19.5 for index in range(288)),
        maximum_temperature_c=(22.0,) * 288,
        terminal_minimum_c=19.5,
        load_is_non_heating=True,
        max_evaluations=12,
    )


def _snapshot(base):
    """Capture caller-owned planning state without copying native prediction handles."""
    return deepcopy({field: getattr(base, field, None) for field in VENUE_FIELDS}), base.prediction


def _unchanged(base, before):
    """Require discarded or failed planning to leave both values and prediction identity alone."""
    values, prediction = before
    assert {field: getattr(base, field, None) for field in VENUE_FIELDS} == values
    assert base.prediction is prediction


def _accounting():
    """Replace embedded heating exactly once and reject ambiguous energy accounting."""
    from predheat_joint import replace_heating_load

    assert replace_heating_load((1.0, 0.5), (0.2, 0.1), (0.3, 0.4)) == (1.1, 0.8)
    for total, embedded, proposed in [((0.1,), (0.2,), (0.1,)), ((1.0,), (), (0.1,)), ((1.0,), (0.0,), (float("nan"),))]:
        try:
            replace_heating_load(total, embedded, proposed)
        except ValueError:
            continue
        raise AssertionError("invalid or unscoped heating accounting was accepted")


def _battery_reoptimises(base):
    """Use real battery optimisation, not a fixed battery replay or a stand-in cost scorer."""
    from predheat_battery import BatteryHeatEvaluator

    base = _engine(base)
    evaluator = BatteryHeatEvaluator(base, non_heating_load_steps=base.load_minutes_step)
    before = _snapshot(base)
    no_heat = evaluator.evaluate((0.0,) * 288)
    evening = tuple(1.5 / 12 if 192 <= index < 240 else 0.0 for index in range(288))
    with_heat = evaluator.evaluate(evening)
    assert with_heat.charge_limit_best != no_heat.charge_limit_best or not isclose(with_heat.final_soc, no_heat.final_soc, abs_tol=0.05)
    assert with_heat.import_kwh > no_heat.import_kwh
    assert with_heat.load_minutes_step[16 * 60] == base.load_minutes_step[16 * 60] + 1.5 / 12
    assert with_heat.battery_value > 0
    assert isclose(with_heat.metric, with_heat.cost - with_heat.battery_value, abs_tol=0.001)
    assert isclose(with_heat.battery_value, with_heat.final_soc * base.battery_value_rate(1440), abs_tol=0.001)
    _unchanged(base, before)
    # Re-evaluation is a battery-only regression, including windows and terminal value.
    repeat = evaluator.evaluate((0.0,) * 288)
    for field in ("metric", "cost", "final_soc", "charge_limit_best", "charge_window_best", "export_limits_best"):
        assert getattr(repeat, field) == getattr(no_heat, field)
    try:
        evaluator.evaluate((0.0,))
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete heat profile accepted")
    _unchanged(base, before)
    # An independent battery-only path calls the native planner directly, not
    # evaluate again, so zero heating cannot silently change the old objective.
    base.prefill_charge_limit_best()
    initial = base.run_prediction_metric(base.charge_limit, base.charge_window, base.export_window, base.export_limits, end_record=1440)
    base.optimise_all_windows(initial[0], initial[3], debug_mode=False)
    base.update_target_values()
    base.end_record = 1440
    native = base.run_prediction_metric(base.charge_limit_best, base.charge_window_best, base.export_window_best, base.export_limits_best, end_record=1440)
    assert isclose(no_heat.metric, native[0], abs_tol=0.001)
    assert isclose(no_heat.cost, native[2], abs_tol=0.001)
    assert no_heat.charge_limit_best == tuple(base.charge_limit_best)
    assert no_heat.charge_window_best == tuple(base.charge_window_best)


def _cheap_deadline(base):
    """Optimise heating and then independently replay its winning battery load."""
    from predheat_battery import BatteryHeatEvaluator
    from predheat_joint import plan_joint_heating

    base = _engine(base)
    request = _request()
    before = _snapshot(base)
    result = plan_joint_heating(base, request)
    assert result.best is not None and result.baseline is not None, result.reason
    assert result.best.battery.metric <= result.baseline.battery.metric + 0.001
    assert 1 < result.evaluations <= request.max_evaluations
    trajectory = result.best.simulation.intervals
    assert trajectory[71].room_temperature_c >= request.minimum_temperature_c[72] - 1e-6
    assert trajectory[-1].room_temperature_c >= request.terminal_minimum_c - 1e-6
    assert result.best.simulation.final_state.water_temperature_c >= request.terminal_water_minimum_c - 1e-6
    assert all(request.minimum_temperature_c[index] - 1e-6 <= row.room_temperature_c <= request.maximum_temperature_c[index] + 1e-6 for index, row in enumerate(trajectory))
    replay = BatteryHeatEvaluator(base, non_heating_load_steps=base.load_minutes_step).evaluate(tuple(row.electrical_kwh for row in trajectory))
    assert isclose(replay.metric, result.best.battery.metric, abs_tol=0.001)
    assert replay.charge_limit_best == result.best.battery.charge_limit_best
    _unchanged(base, before)
    print("Joint heating deadline metric {}, baseline {}, terminal SOC {}".format(result.best.battery.metric, result.baseline.battery.metric, result.best.battery.final_soc))


def _pv_export(base):
    """Real PV self-consumption and export value affect equal-energy heating candidates."""
    from predheat_battery import BatteryHeatEvaluator

    morning = tuple(1.0 / 12 if 72 <= index < 120 else 0.0 for index in range(288))
    midday = tuple(1.0 / 12 if 132 <= index < 180 else 0.0 for index in range(288))
    values = []
    for export_price in (0.0, 35.0):
        base = _engine(base, pv=True, export_price=export_price)
        evaluator = BatteryHeatEvaluator(base, non_heating_load_steps=base.load_minutes_step)
        early = evaluator.evaluate(morning)
        solar = evaluator.evaluate(midday)
        values.append(early.metric - solar.metric)
        assert solar.export_kwh < early.export_kwh or solar.import_kwh < early.import_kwh
    assert not isclose(values[0], values[1], abs_tol=0.05), values


def _joint_failure(base):
    """An infeasible comfort policy or ambiguous load ownership cannot adopt planning state."""
    from predheat_joint import plan_joint_heating

    base = _engine(base)
    before = _snapshot(base)
    bad = replace(_request(), minimum_temperature_c=(25.0,) * 288, maximum_temperature_c=(26.0,) * 288)
    result = plan_joint_heating(base, bad)
    assert result.best is None
    _unchanged(base, before)
    try:
        result = plan_joint_heating(base, replace(_request(), load_is_non_heating=False))
    except ValueError:
        pass
    else:
        assert result.best is None
    _unchanged(base, before)


class _HeatingSnapshot:
    """Input-boundary adapter supplying real pure-model data without sensor or control calls."""

    def __init__(self, base):
        """Hold the exact timestamp and comfort schedule the standard adapter supplies."""
        self.base = base
        self.model_input = _thermal()
        self.comfort_target_temperature_c = self.model_input.target_temperature_c
        self.model_input_updated_at = base.now_utc
        self.had_errors = False
        self.prefix = "predheat_test"
        self.updates = []
        self.refresh_timestamp = True
        self.refresh_error = None

    def is_enabled(self):
        """Expose an enabled heating input source at the adapter boundary."""
        return True

    def update_pred(self, scheduled=False, plan_at=None):
        """Record one refresh without publishing or replacing the thermal or battery models."""
        assert plan_at == self.base.now_utc
        self.updates.append(scheduled)
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refresh_timestamp:
            self.model_input_updated_at = self.base.now_utc


@contextmanager
def _runtime_source(base):
    """Restore the input adapter, configuration and test output store after each bridge case."""
    old_args = base.args
    sentinel = object()
    old_source = getattr(base, "predheat", sentinel)
    old_summary = getattr(base, "joint_heating_plan", sentinel)
    old_entities = deepcopy(base.ha_interface.dummy_items)
    old_services = list(base.ha_interface.service_store)
    old_record_services = base.ha_interface.service_store_enable
    base.ha_interface.service_store_enable = True
    base.args = dict(base.args)
    base.args["predheat"] = {
        "joint_planning": True,
        "joint_load_is_non_heating": True,
        "joint_comfort_margin": 0.7,
        "joint_overheat_margin": 1.8,
        "joint_terminal_temperature": 19.5,
        "joint_max_evaluations": 12,
    }
    adapter = _HeatingSnapshot(base)
    base.predheat = adapter
    try:
        with patch("predheat_shadow.datetime", wraps=datetime, create=True) as clock:
            clock.now.return_value = base.now_utc
            yield adapter
    finally:
        base.args = old_args
        for name, old in (("predheat", old_source), ("joint_heating_plan", old_summary)):
            if old is sentinel:
                if hasattr(base, name):
                    delattr(base, name)
            else:
                setattr(base, name, old)
        base.ha_interface.dummy_items = old_entities
        base.ha_interface.service_store = old_services
        base.ha_interface.service_store_enable = old_record_services


def _runtime_series(base):
    """Keep three deliberately distinct baseline scenarios so indexing mistakes are visible."""
    loads = tuple({minute: multiplier * value for minute, value in base.load_minutes_step.items()} for multiplier in (1, 1.5, 0.5))
    solar = (base.pv_forecast_minute_step, base.pv_forecast_minute10_step, base.pv_forecast_minute90_step)
    return loads, solar


def _runtime_baseline(base):
    """Replays add native heat once to every scenario, without refresh or output side effects."""
    from predheat_model import simulate_heating
    from predheat_shadow import prepare_joint_heating_shadow

    base = _engine(base)
    loads, solar = _runtime_series(base)
    source_loads = deepcopy(loads)
    before = _snapshot(base)
    with _runtime_source(base) as adapter:
        entities = deepcopy(base.ha_interface.dummy_items)
        services = list(base.ha_interface.service_store)
        expected = simulate_heating(adapter.model_input)
        combined = prepare_joint_heating_shadow(base, loads, solar, evaluate=False)
        repeated = prepare_joint_heating_shadow(base, loads, solar, evaluate=False)
        assert combined == repeated
        assert loads == source_loads
        assert not adapter.updates
        assert base.ha_interface.dummy_items == entities
        assert base.ha_interface.service_store == services
        assert not hasattr(base, "joint_heating_plan")
        for scenario, values in enumerate(combined):
            for index, row in enumerate(expected.intervals):
                minute = index * 5
                assert isclose(values[minute], loads[scenario][minute] + row.electrical_kwh, abs_tol=1e-12)
        _unchanged(base, before)


def _runtime_gates(base):
    """Disabled configuration is identity; ambiguous loads and stale snapshots fail before output."""
    from predheat_shadow import prepare_joint_heating_shadow

    base = _engine(base)
    loads, solar = _runtime_series(base)
    before = _snapshot(base)
    with _runtime_source(base) as adapter:
        base.args["predheat"]["joint_planning"] = False
        assert prepare_joint_heating_shadow(base, loads, solar) is loads
        assert not adapter.updates
        base.args["predheat"]["joint_planning"] = True
        for flag in (None, False, "true"):
            base.args["predheat"]["joint_load_is_non_heating"] = flag
            try:
                prepare_joint_heating_shadow(base, loads, solar)
            except ValueError:
                pass
            else:
                raise AssertionError("unqualified household load was accepted")
        assert not adapter.updates
        base.args["predheat"]["joint_load_is_non_heating"] = True
        entities = deepcopy(base.ha_interface.dummy_items)
        adapter.refresh_timestamp = False
        for timestamp in (base.now_utc - timedelta(minutes=2), base.now_utc + timedelta(minutes=2), base.now_utc.replace(tzinfo=None)):
            adapter.model_input_updated_at = timestamp
            try:
                prepare_joint_heating_shadow(base, loads, solar, evaluate=False)
            except ValueError:
                pass
            else:
                raise AssertionError("stale, future or timezone-less snapshot was accepted")
        assert base.ha_interface.dummy_items == entities
        _unchanged(base, before)


def _runtime_preview(base):
    """Preview refreshes once and publishes real joint metrics without adopting a live plan."""
    from predheat_model import simulate_heating
    from predheat_shadow import prepare_joint_heating_shadow

    base = _engine(base)
    loads, solar = _runtime_series(base)
    before = _snapshot(base)
    with _runtime_source(base) as adapter:
        adapter.model_input_updated_at -= timedelta(hours=1)
        services = list(base.ha_interface.service_store)
        combined = prepare_joint_heating_shadow(base, loads, solar, evaluate=True)
        assert adapter.updates == [False]
        assert adapter.model_input_updated_at == base.now_utc
        summary = base.joint_heating_plan
        assert summary["state"] == "planned"
        assert summary["shadow_only"] is True
        assert summary["baseline_comparable"] is True
        assert 1 < summary["evaluations"] <= 12
        assert 1 <= summary["battery_evaluations"] <= summary["evaluations"]
        assert summary["metric"] <= summary["baseline_metric"] + 0.001
        assert summary["battery_value"] > 0
        assert summary["terminal_soc_kwh"] >= base.reserve - 1e-6
        assert len(summary["results"]) == 288
        assert base.ha_interface.get_state(adapter.prefix + ".joint_plan") == "planned"
        assert base.ha_interface.get_state(adapter.prefix + ".joint_plan", attribute="shadow_only") is True
        assert base.ha_interface.get_state(adapter.prefix + ".joint_plan", attribute="metric") == summary["metric"]
        assert base.ha_interface.service_store == services
        baseline = simulate_heating(adapter.model_input)
        for scenario, values in enumerate(combined):
            for index, row in enumerate(baseline.intervals):
                assert isclose(values[index * 5], loads[scenario][index * 5] + row.electrical_kwh, abs_tol=1e-12)
        _unchanged(base, before)


def _runtime_full_plan(base):
    """The full native planner consumes baseline heat and publishes only a joint preview."""
    from predheat_model import simulate_heating

    base = _engine(base)
    base.calculate_savings = False
    base.load_forecast_only = True
    base.load_forecast = {}
    base.load_minutes = {}
    base.pv_forecast_minute = {minute: 0.0 for minute in range(1440)}
    base.pv_forecast_minute10 = dict(base.pv_forecast_minute)
    base.pv_forecast_minute90 = dict(base.pv_forecast_minute)
    base.plan_valid = False
    base.plan_last_updated_minutes = 0
    base.update_pending = False
    with _runtime_source(base) as adapter:
        expected = simulate_heating(adapter.model_input)
        services = list(base.ha_interface.service_store)
        recomputed = base.calculate_plan(recompute=True, publish=True)
        assert recomputed is True
        assert base.plan_valid is True
        assert adapter.updates == [False]
        assert base.joint_heating_plan["shadow_only"] is True
        assert base.joint_heating_plan["state"] == "planned"
        assert base.ha_interface.service_store == services
        # Raw household/PV forecasts are empty here, so every final load kWh
        # must come from native heating, never the optimised preview profile.
        for index, row in enumerate(expected.intervals):
            for field in ("load_minutes_step", "load_minutes_step10", "load_minutes_step90"):
                assert isclose(getattr(base, field)[index * 5], row.electrical_kwh, abs_tol=1e-12)
        # A comparison calculation uses the same fresh input snapshot without
        # refreshing PredHeat or emitting another joint preview.
        summary = deepcopy(base.joint_heating_plan)
        published = deepcopy(base.ha_interface.dummy_items[adapter.prefix + ".joint_plan"])
        base.calculate_plan(recompute=True, publish=False)
        assert adapter.updates == [False]
        assert base.joint_heating_plan == summary
        assert base.ha_interface.dummy_items[adapter.prefix + ".joint_plan"] == published
        # Five minutes later a live cycle may reuse the battery schedule, but
        # heating inputs must be refreshed rather than replayed at the old origin.
        base.minutes_now += 5
        base.now_utc += timedelta(minutes=5)
        adapter.model_input = replace(adapter.model_input, initial_state=replace(adapter.model_input.initial_state, room_temperature_c=20.8))
        expected = simulate_heating(adapter.model_input)
        with patch("predheat_shadow.datetime", wraps=datetime) as live_clock:
            live_clock.now.return_value = base.now_utc
            assert base.calculate_plan(recompute=False, publish=True) is False
        assert adapter.updates == [False, False]
        assert adapter.model_input_updated_at == base.now_utc
        assert base.plan_valid is True
        assert base.joint_heating_plan == summary
        assert base.ha_interface.dummy_items[adapter.prefix + ".joint_plan"] == published
        for index, row in enumerate(expected.intervals):
            for field in ("load_minutes_step", "load_minutes_step10", "load_minutes_step90"):
                assert isclose(getattr(base, field)[index * 5], row.electrical_kwh, abs_tol=1e-12)
        base.args["predheat"]["joint_planning"] = False
        base.calculate_plan(recompute=True, publish=True)
        assert adapter.updates == [False, False]
        assert base.joint_heating_plan["state"] == "disabled"
        assert "metric" not in base.joint_heating_plan
        assert "results" not in base.joint_heating_plan
        assert base.ha_interface.get_state(adapter.prefix + ".joint_plan") == "disabled"
        assert "results" not in base.ha_interface.dummy_items[adapter.prefix + ".joint_plan"]


def _runtime_invalidation(base):
    """Input and refresh failures replace old successful previews, preserving live planning state."""
    from predheat_shadow import prepare_joint_heating_shadow

    base = _engine(base)
    loads, solar = _runtime_series(base)
    before = _snapshot(base)
    with _runtime_source(base) as adapter:
        for failure in ("input", "refresh"):
            adapter.had_errors = False
            adapter.refresh_error = None
            prepare_joint_heating_shadow(base, loads, solar)
            assert base.joint_heating_plan["state"] == "planned"
            assert "results" in base.joint_heating_plan
            if failure == "input":
                adapter.had_errors = True
            else:
                adapter.refresh_error = RuntimeError("input adapter could not refresh")
            try:
                prepare_joint_heating_shadow(base, loads, solar)
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("failed input refresh was accepted")
            summary = base.joint_heating_plan
            assert summary["state"] == "unavailable"
            assert summary["reason"] == "input_or_evaluation_error"
            assert "results" not in summary and "metric" not in summary
            assert base.ha_interface.get_state(adapter.prefix + ".joint_plan") == "unavailable"
            assert "results" not in base.ha_interface.dummy_items[adapter.prefix + ".joint_plan"]
            _unchanged(base, before)


def _runtime_live_clock(base):
    """A historical planning clock cannot trigger live sensor refresh, and invalidates old preview."""
    from predheat_shadow import prepare_joint_heating_shadow

    base = _engine(base)
    loads, solar = _runtime_series(base)
    before = _snapshot(base)
    with _runtime_source(base) as adapter:
        prepare_joint_heating_shadow(base, loads, solar)
        updates = list(adapter.updates)
        assert base.joint_heating_plan["state"] == "planned"
        with patch("predheat_shadow.datetime", wraps=datetime) as live_clock:
            live_clock.now.return_value = base.now_utc + timedelta(hours=2)
            try:
                prepare_joint_heating_shadow(base, loads, solar)
            except ValueError as error:
                assert "cycle started within 30 minutes" in str(error)
            else:
                raise AssertionError("historical replay triggered live refresh")
        assert adapter.updates == updates
        assert base.joint_heating_plan["state"] == "unavailable"
        assert "results" not in base.joint_heating_plan
        _unchanged(base, before)


def _joint_deadline(base):
    """Validate soft deadlines and stop new trials without discarding the real evaluated baseline."""
    from predheat_joint import plan_joint_heating

    for value in (True, 0, -1, 0.5, 301, float("nan"), float("inf"), "30"):
        try:
            replace(_request(), max_seconds=value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid time budget accepted: {}".format(value))
    base = _engine(base)
    before = _snapshot(base)
    request = replace(_request(), max_seconds=1)
    # Only the elapsed-time source is controlled. Physics and the first battery
    # optimisation run normally, then no second thermal trial may begin.
    with patch("predheat_joint.monotonic", side_effect=chain((0.0, 0.0), repeat(2.0))):
        result = plan_joint_heating(base, request)
    assert result.termination == "time_budget"
    assert result.evaluations == 1
    assert result.battery_evaluations == 1
    assert result.best is not None and result.baseline is not None
    assert result.best.battery.metric == result.baseline.battery.metric
    _unchanged(base, before)


def _legacy_power_clamp(base):
    """Existing PredHeat users retain the old output clamp when minimum exceeds maximum."""
    from predheat_model import simulate_heating
    from tests.test_predheat import _make_simulation, BASE_TEMP

    adapted = _make_simulation(base, [], smart_thermostat=False)
    adapted.heat_min_power = 7000
    adapted.heat_max_power = 5000
    adapted.heat_pump_efficiency = {5: 1.0}
    adapted.heat_pump_efficiency_max = 1.0
    adapted.run_simulation(BASE_TEMP, True, save="none")
    assert adapted.heat_min_power == 7000
    observed = simulate_heating(adapted.model_input)
    # The original max(minimum, output), then min(maximum, output) order
    # gives exactly 5 kW on the first interval, regardless of the higher minimum.
    assert isclose(observed.intervals[0].heat_generated_kwh, 5000 / 12000)
    assert isclose(observed.intervals[0].electrical_kwh, 5000 / 12000 / adapted.heat_cop)
    assert all(row.heat_generated_kwh <= 5000 / 12000 + 1e-9 for row in observed.intervals)


def _runtime_planning_clock(base):
    """Native history, weather and physics share the host clock despite skew or a boundary crossing."""
    from predheat import PredHeat
    from predheat_shadow import prepare_joint_heating_shadow

    base = _engine(base)
    loads, solar = _runtime_series(base)
    with _runtime_source(base):
        base.args["clock_skew"] = -2
        adapter = PredHeat(base)
        adapter.reset()
        adapter.is_enabled = lambda: True
        base.predheat = adapter
        reads = []

        def history(now, key, **kwargs):
            """Supply sensor history at the requested origin, recording every lookup clock."""
            reads.append(now)
            return ({}, 0) if key == "heating_energy" else ({0: 20.0, 7 * 1440: 20.0}, 7)

        adapter.minute_data_entity = history
        adapter.temperatures = {}
        adapter.get_weather_data = lambda now: reads.append(now)
        adapter.today_cost = lambda history: 0
        with patch("predheat_shadow.datetime", wraps=datetime) as live_clock, patch("predheat.datetime", wraps=datetime) as separate_clock:
            live_clock.now.return_value = base.now_utc + timedelta(minutes=2, seconds=5)
            separate_clock.now.return_value = base.now_utc + timedelta(minutes=5)
            combined = prepare_joint_heating_shadow(base, loads, solar, evaluate=False, refresh_inputs=True)
            separate_clock.now.assert_not_called()
        assert reads and all(now == base.now_utc for now in reads)
        assert adapter.model_input_updated_at == base.now_utc
        assert len(combined[0]) == base.forecast_minutes // 5
        # Slow fetches and a second calculation in one cycle must not fail
        # just because more than one five-minute slot has elapsed.
        with patch("predheat_shadow.datetime", wraps=datetime) as delayed_clock:
            delayed_clock.now.return_value = base.now_utc_real + timedelta(minutes=12)
            prepare_joint_heating_shadow(base, loads, solar, evaluate=False, refresh_inputs=True)
        # A normal cycle's planning clock includes skew, but qualification must
        # use its real origin rather than rejecting the shifted planning time.
        base.now_utc = base.now_utc_real + timedelta(minutes=60)
        base.minutes_now += 60
        with patch("predheat_shadow.datetime", wraps=datetime) as skew_clock:
            skew_clock.now.return_value = base.now_utc_real
            prepare_joint_heating_shadow(base, loads, solar, evaluate=False, refresh_inputs=True)
        assert adapter.model_input_updated_at == base.now_utc
        base.now_utc = base.now_utc_real
        base.minutes_now -= 60
        # plan_once(plan_at=...) ignores configured skew. The bridge must too:
        # live-cycle qualification uses the origin without skew, not args.clock_skew.
        base.args["clock_skew"] = 60
        with patch("predheat_shadow.datetime", wraps=datetime) as explicit_clock:
            explicit_clock.now.return_value = base.now_utc_real
            prepare_joint_heating_shadow(base, loads, solar, evaluate=False, refresh_inputs=True)
        reads.clear()
        try:
            adapter.update_pred(scheduled=False, plan_at=base.now_utc.replace(tzinfo=None))
        except ValueError:
            pass
        else:
            raise AssertionError("naive explicit planning origin was accepted")
        assert not reads


def run_joint_heat_tests(my_predbat):
    """Run public engine integration scenarios using the runner's real PredBat fixture."""
    failed = False
    for name, scenario in (
        ("accounting", lambda: _accounting()),
        ("legacy_power_clamp", lambda: _legacy_power_clamp(my_predbat)),
        ("battery_re-optimises", lambda: _battery_reoptimises(my_predbat)),
        ("cheap_deadline", lambda: _cheap_deadline(my_predbat)),
        ("pv_export", lambda: _pv_export(my_predbat)),
        ("joint_failure", lambda: _joint_failure(my_predbat)),
        ("runtime_baseline", lambda: _runtime_baseline(my_predbat)),
        ("runtime_gates", lambda: _runtime_gates(my_predbat)),
        ("runtime_preview", lambda: _runtime_preview(my_predbat)),
        ("runtime_full_plan", lambda: _runtime_full_plan(my_predbat)),
        ("runtime_invalidation", lambda: _runtime_invalidation(my_predbat)),
        ("runtime_live_clock", lambda: _runtime_live_clock(my_predbat)),
        ("runtime_planning_clock", lambda: _runtime_planning_clock(my_predbat)),
        ("joint_deadline", lambda: _joint_deadline(my_predbat)),
    ):
        try:
            scenario()
            print("Joint heating {} passed".format(name))
        except Exception as error:
            import traceback

            traceback.print_exc()
            print("ERROR: Joint heating {}: {}".format(name, error))
            failed = True
    return failed
