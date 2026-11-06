# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Offline checks of import diagnostics, not of hard-limit enforcement or EV planning."""

from copy import deepcopy
from datetime import timedelta
import os
from unittest.mock import patch

from const import IMPORT_LIMIT_DEFAULT_W, PREDICT_STEP, PREDBAT_MAX_CARS, TIME_FORMAT
from prediction import Prediction
from prediction_kernel import create_kernel_context, kernel_supported, reset_kernel_run_state, run_prediction_kernel
from tests.test_infra import FIXTURE_MINUTES_NOW, reset_inverter, reset_rates


def _close(actual, expected, tolerance=1e-6):
    """Assert a numerical result with an explicit, unit-appropriate tolerance."""
    assert abs(actual - expected) <= tolerance, (actual, expected, tolerance)


def _fixture(base, limit=6000, load=1.2, pv=0, ev=0, reported=True, minutes=60):
    """Build a synthetic forecast; reserve is kWh, efficiencies are fractions, rates kW."""
    base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    reset_inverter(base)
    base.forecast_minutes = minutes
    base.end_record = minutes
    base.minutes_now = FIXTURE_MINUTES_NOW
    base.now_utc = base.midnight_utc + timedelta(minutes=base.minutes_now)
    reset_rates(base, 10, 0)
    base.prediction_kernel_enable = False
    base.debug_enable = False
    base.import_limit = float(limit)
    base.soc_max = 32.1536
    base.reserve = base.soc_max * 0.10
    base.soc_kw = base.reserve
    base.inverter_limit = 16 / 60
    base.battery_rate_max_charge = 3 / 60
    base.battery_rate_max_charge_dc = 3 / 60
    base.battery_rate_max_discharge = 8.192 / 60
    base.battery_rate_max_export = 8.192 / 60
    base.inverter_loss = 0.95
    base.battery_loss = base.battery_loss_discharge = 0.96
    base.battery_temperature = 20
    base.battery_temperature_prediction = {}
    base.battery_temperature_charge_curve = {}
    base.battery_temperature_discharge_curve = {}
    base.best_soc_max = -1
    base.best_soc_min = 0
    base.all_active_keep = {}
    base.all_active_keep_max = {}
    base.io_adjusted = {}
    base.set_charge_low_power = False
    base.set_export_low_power = False
    base.metric_standing_charge = 0
    base.export_today_now = base.import_today_now = base.pv_today_now = base.load_minutes_now = 0
    base.cost_today_sofar = base.carbon_today_sofar = 0
    base.iboost_today = 0
    base.iboost_plan = []
    base.num_cars = 1
    base.car_charging_limit_model = None
    base.car_charging_limit = [25.0] * PREDBAT_MAX_CARS
    base.car_charging_battery_size = [25.0] * PREDBAT_MAX_CARS
    base.car_charging_loss = 0.92
    base.car_charging_from_battery = True
    base.car_energy_reported_load = reported
    if ev:
        base.car_charging_slots[0] = [{"start": base.minutes_now, "end": base.minutes_now + minutes, "kwh": ev * minutes / 60}]
    pv_steps = {m: pv * PREDICT_STEP / 60 for m in range(0, minutes, PREDICT_STEP)}
    load_steps = {m: load * PREDICT_STEP / 60 for m in range(0, minutes, PREDICT_STEP)}
    return Prediction(base, pv_steps, pv_steps, load_steps, load_steps)


def _run(pred, save="best", step=PREDICT_STEP, end_record=None, charge=False, cache=False):
    """Run the real engine, optionally with a native whole-horizon charge window."""
    windows = [{"start": pred.minutes_now, "end": pred.minutes_now + pred.forecast_minutes, "average": 10}] if charge else []
    return pred.run_prediction([pred.soc_max] if charge else [], windows, [], [], 0, pred.forecast_minutes if end_record is None else end_record, save=save, step=step, cache=cache)


def _snapshot(pred, **kwargs):
    """Capture the entire return tuple and existing saved curves for equivalence checks."""
    result = _run(pred, **kwargs)
    curves = {name: getattr(pred, name) for name in ("predict_grid_power", "predict_battery_power", "predict_load_power", "predict_pv_power", "predict_soc", "predict_battery_cycle")}
    return deepcopy((result, curves))


def test_config(base):
    """Exercise actual configuration fetch, invalid input, reloads and multi-inverter scope."""
    saved = base.args.copy()
    try:
        for count in (1, 3):
            base.args["num_inverters"] = count
            for limit in (0, 6000, 4000, 9000.5, 0):
                base.args["import_limit"] = limit
                base.validate_config()
                assert "import_limit" not in base.arg_errors
                base.fetch_config_options()
                assert base.import_limit == limit
            base.args.pop("import_limit")
            base.fetch_config_options()
            assert base.import_limit == IMPORT_LIMIT_DEFAULT_W
        for bad in (True, False, None, "6000", [6000], {}, -1, float("nan"), float("inf"), -float("inf")):
            base.args["import_limit"] = bad
            try:
                base.fetch_config_options()
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid import limit accepted: {!r}".format(bad))
    finally:
        base.args.clear()
        base.args.update(saved)
        base.fetch_config_options()
    fresh = type(base)()
    fresh.import_limit = 9000
    fresh.reset()
    assert fresh.import_limit == 0


def test_fixed_ev_at_reserve(base):
    """A 6.77 kW EV plus 1.2 kW house cannot be supplied by a battery already at 10%."""
    for reported in (True, False):
        pred = _fixture(base, limit=0, ev=6.77, reported=reported)
        baseline = _snapshot(pred)
        for limit in (0, 4000, 6000, 9000):
            pred.import_limit = limit
            assert _snapshot(pred) == baseline
            _close(pred.final_soc, 3.21536, 0.00005)
            _close(pred.final_battery_cycle, 0)
            _close(pred.final_import_kwh, 7.97 if reported else 1.2)
            _close(pred.final_car_soc[0], 6.77 * 0.92, 0.0005)
            assert all(power == 0 for power in pred.predict_battery_power.values())
            diagnostic = pred.import_limit_diagnostics
            if limit == 0:
                assert diagnostic is None
            else:
                _close(diagnostic["peak_w"], 7970)
                _close(diagnostic["excess_kwh"], max(7970 - limit, 0) / 1000)
                assert diagnostic["minutes_over"] == (60 if limit < 7970 else 0)
        # Both CT configurations see the same site demand; the existing CT graph is not rewritten.
        assert set(pred.predict_grid_power.values()) == ({7.97} if reported else {1.2})


def test_available_battery_balance(base):
    """Available battery support respects energy, losses, inverter power and native dispatch."""
    pred = _fixture(base, limit=0, load=10)
    pred.soc_kw = 8
    pred.inverter_limit = 2 / 60
    baseline = _snapshot(pred)
    pred.import_limit = 6000
    assert _snapshot(pred) == baseline
    assert pred.reserve <= pred.final_soc < pred.soc_kw
    _close((pred.soc_kw - pred.final_soc) * pred.battery_loss_discharge, pred.final_battery_cycle, 0.0002)
    _close(pred.final_import_kwh + pred.final_battery_cycle * pred.inverter_loss, 10, 0.0002)
    assert max(pred.predict_battery_power.values()) <= 8.192
    assert max(pred.predict_battery_power.values()) * pred.inverter_loss <= 2.001
    assert pred.import_limit_diagnostics["minutes_over"] == 60


def test_bypass_pv_netting(base):
    """PV surplus meets outside-CT EV demand before computing net site import."""
    for reported in (True, False):
        pred = _fixture(base, limit=1000, pv=6, ev=6.77, reported=reported)
        pred.battery_rate_max_charge = pred.battery_rate_max_charge_dc = pred.battery_rate_max_discharge = 0
        _run(pred)
        _close(pred.import_limit_diagnostics["peak_w"], 1970)
        _close(pred.import_limit_diagnostics["excess_kwh"], 0.97)
        _close(pred.final_battery_cycle, 0)


def test_charging_unchanged(base):
    """Grid charge above the threshold is reported, while PV charging and energy stay intact."""
    for hybrid in (False, True):
        for pv, load in ((0, 5), (6, 1)):
            pred = _fixture(base, limit=0, load=load, pv=pv)
            pred.inverter_hybrid = hybrid
            baseline = _snapshot(pred, charge=True)
            pred.import_limit = 4000
            assert _snapshot(pred, charge=True) == baseline
            _close(pred.final_soc - pred.soc_kw, 3 * 0.96, 0.0001)
            diagnostic = pred.import_limit_diagnostics
            if pv:
                assert diagnostic["peak_w"] == 0
                assert diagnostic["excess_kwh"] == 0
            else:
                _close(diagnostic["peak_w"], (5 + 3 / 0.95) * 1000)
                _close(diagnostic["excess_kwh"], 5 + 3 / 0.95 - 4)
                assert diagnostic["minutes_over"] == 60


def test_no_ev_and_empty_battery(base):
    """No EV slots means no EV demand; empty or absent batteries cannot create energy."""
    pred = _fixture(base)
    _run(pred)
    _close(pred.import_limit_diagnostics["peak_w"], 1200)
    assert pred.import_limit_diagnostics["excess_kwh"] == 0
    for capacity in (32.1536, 0):
        pred = _fixture(base, load=7)
        pred.soc_max = capacity
        pred.soc_kw = pred.reserve = 0
        if capacity == 0:
            pred.battery_rate_max_charge = pred.battery_rate_max_charge_dc = pred.battery_rate_max_discharge = pred.battery_rate_max_export = 0
        _run(pred)
        _close(pred.final_import_kwh, 7)
        _close(pred.final_battery_cycle, 0)
        _close(pred.import_limit_diagnostics["peak_w"], 7000)
        _close(pred.import_limit_diagnostics["excess_kwh"], 1)


def test_step_and_record_scope(base):
    """Step conversion is correct and diagnostics cover the forecast beyond end_record."""
    for step in (5, 10, 30):
        pred = _fixture(base, load=7)
        _run(pred, step=step, end_record=5)
        diagnostic = pred.import_limit_diagnostics
        assert diagnostic["step_minutes"] == step
        assert diagnostic["checked_minutes"] == diagnostic["minutes_over"] == 60
        _close(diagnostic["peak_w"], 7000)
        _close(diagnostic["excess_kwh"], 1)
        assert diagnostic["start"] == (pred.midnight_utc + timedelta(minutes=pred.minutes_now)).strftime(TIME_FORMAT)
        assert diagnostic["end"] == (pred.midnight_utc + timedelta(minutes=pred.minutes_now + 60)).strftime(TIME_FORMAT)


def test_raw_boundary_and_first_excess(base):
    """Compare before graph rounding, ignore numerical noise and stamp the first violating step."""
    for load, over in ((6, False), (6.0000000005, False), (6.0000001, True), (6.0001, True)):
        pred = _fixture(base, load=load, minutes=5)
        _run(pred)
        diagnostic = pred.import_limit_diagnostics
        assert diagnostic["minutes_over"] == (5 if over else 0)
        _close(diagnostic["excess_kwh"], (load - 6) * 5 / 60 if over else 0, 1e-12)
        assert set(pred.predict_grid_power.values()) == {6.0}
    pred = _fixture(base, load=5)
    for minute in range(30, 60, PREDICT_STEP):
        pred.load_minutes_step[minute] = 7 * PREDICT_STEP / 60
    _run(pred)
    diagnostic = pred.import_limit_diagnostics
    assert diagnostic["first_exceeded"] == (pred.midnight_utc + timedelta(minutes=pred.minutes_now + 30)).strftime(TIME_FORMAT)
    assert diagnostic["minutes_over"] == 30
    _close(diagnostic["excess_kwh"], 0.5)


def test_prices_do_not_change_diagnostics(base):
    """Negative, zero and positive prices retain costs and cannot change constraint diagnostics."""
    diagnostics = []
    for rate in (-5, 0, 25):
        pred = _fixture(base, limit=0, load=7)
        pred.rate_import = {minute: rate for minute in pred.rate_import}
        baseline = _snapshot(pred)
        pred.import_limit = 6000
        assert _snapshot(pred) == baseline
        _close(pred.final_metric, 7 * rate)
        diagnostics.append(pred.import_limit_diagnostics)
    assert diagnostics[0] == diagnostics[1] == diagnostics[2]


def test_save_cache_and_real_kernel_reset(base):
    """Exercise a real cache hit and native kernel dispatch, not a patch on an unused import."""
    pred = _fixture(base, load=7)
    for save in ("best", "base", "best10"):
        _run(pred, save=save)
        assert pred.import_limit_diagnostics["minutes_over"] == 60
    trial = _run(pred, save=None, cache=True)
    assert pred.import_limit_diagnostics is None
    assert len(pred.prediction_cache) == 1
    _run(pred)
    # A cache hit must return before querying kernel support; this fails if no cache is used.
    with patch("prediction.kernel_supported", side_effect=AssertionError("Expected a cache hit")):
        cached = _run(pred, save=None, cache=True)
    assert cached[:11] == trial[:11]
    assert pred.import_limit_diagnostics is None
    pred.kernel_handle = create_kernel_context(pred)
    if pred.kernel_handle:
        for limit in (0, 6000, 99999):
            pred.import_limit = limit
            assert kernel_supported(pred, save=None, step=5)
            assert not kernel_supported(pred, save="best", step=5)
            _run(pred)
            with patch("prediction.run_prediction_kernel", wraps=run_prediction_kernel) as native:
                result = _run(pred, save=None)
            assert native.call_count == 1
            assert result[:11] == trial[:11]
            assert pred.import_limit_diagnostics is None
    else:
        assert not os.environ.get("PREDBAT_KERNEL_REQUIRED"), "Native prediction kernel required but unavailable"
        print("SKIP: native import diagnostic reset check (kernel unavailable); Python/cache checks ran")
    pred.import_limit_diagnostics = {"stale": True}
    reset_kernel_run_state(pred)
    assert pred.import_limit_diagnostics is None
    pred.import_limit = 0
    _run(pred)
    assert pred.import_limit_diagnostics is None


def test_publication_states(base):
    """Missing, partial, mismatched, empty or disabled diagnostics never appear successful."""
    pred = _fixture(base, load=7)
    _run(pred)
    valid = deepcopy(pred.import_limit_diagnostics)
    key = base.prefix + ".best_import_limit"
    for diagnostic in (valid, None, dict(valid, checked_minutes=30), dict(valid, limit_w=5000)):
        pred.import_limit_diagnostics = diagnostic
        base.publish_import_limit_diagnostics(pred)
        item = base.dashboard_values[key]
        assert item["state"] == ("exceeded" if diagnostic == valid else "unavailable")
        assert item["attributes"]["enforced"] is False
        assert item["attributes"]["scope"] == "whole_site"
        if diagnostic != valid:
            for field in ("peak_w", "excess_kwh", "minutes_over", "first_exceeded", "start", "end"):
                assert item["attributes"][field] is None
    pred.forecast_minutes = 0
    _run(pred)
    assert pred.import_limit_diagnostics["checked_minutes"] == 0
    base.publish_import_limit_diagnostics(pred)
    assert base.dashboard_values[key]["state"] == "unavailable"
    base.import_limit = 0
    base.publish_import_limit_diagnostics(pred)
    assert base.dashboard_values[key]["state"] == "disabled"
    assert base.dashboard_values[key]["attributes"]["excess_kwh"] is None


def test_actual_plan_publication(base):
    """Call the real Plan wrapper: only best saves publish, and the mocked HA receives the state."""
    key = base.prefix + ".best_import_limit"
    for load, expected in ((7, "exceeded"), (4, "within_limit")):
        pred = _fixture(base, load=load, minutes=1440)
        base.prediction = pred
        base.dashboard_values.pop(key, None)
        for save in ("base", "base10", "best10"):
            base.run_prediction([], [], [], [], 0, end_record=1440, save=save)
            assert key not in base.dashboard_values
        base.run_prediction([], [], [], [], 0, end_record=1440, save="best")
        item = base.dashboard_values[key]
        assert item["state"] == expected
        assert item["attributes"]["checked_minutes"] == 1440
        assert base.ha_interface.get_state(key) == expected
        _close(item["attributes"]["excess_kwh"], max(load - 6, 0) * 24)
        base.import_limit = pred.import_limit = 0
        base.run_prediction([], [], [], [], 0, end_record=1440, save="best")
        assert base.dashboard_values[key]["state"] == "disabled"
        assert base.ha_interface.get_state(key) == "disabled"


def run_import_limit_tests(my_predbat):
    """Run the focused diagnostic regressions on the runner's isolated PredBat instance."""
    tests = (
        test_config,
        test_fixed_ev_at_reserve,
        test_available_battery_balance,
        test_bypass_pv_netting,
        test_charging_unchanged,
        test_no_ev_and_empty_battery,
        test_step_and_record_scope,
        test_raw_boundary_and_first_excess,
        test_prices_do_not_change_diagnostics,
        test_save_cache_and_real_kernel_reset,
        test_publication_states,
        test_actual_plan_publication,
    )
    for test in tests:
        test(my_predbat)
        print("PASS:", test.__name__)
    print("Import diagnostics: {} focused checks passed".format(len(tests)))
    return False
