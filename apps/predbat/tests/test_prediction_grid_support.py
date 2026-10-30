"""Independent physical, loss, boundary and default-off regressions for shadow replay."""

from copy import copy
from unittest.mock import patch

from prediction import Prediction
from tests.test_car_planning_native import _fixture


def _prediction(base, load=6.52, pv=0, soc=5, support=6000, charge=3, discharge=8.192, inverter=16, loss=0.96, battery_loss=0.97, hybrid=True, kernel=False, minutes=5):
    """Use real native construction and five-minute forecasts, never patch physics after construction."""
    forecasts = _fixture(base, load=load, pv=pv, minutes=minutes)
    model = copy(base)
    model.num_cars = 0
    model.soc_max, model.reserve, model.soc_kw = 10.0, 1.0, soc
    model.inverter_hybrid, model.inverter_loss = hybrid, loss
    model.battery_loss = model.battery_loss_discharge = battery_loss
    model.charge_rate_now = model.battery_rate_max_charge = model.battery_rate_max_charge_dc = charge / 60
    model.discharge_rate_now = model.battery_rate_max_discharge = discharge / 60
    model.inverter_limit = inverter / 60
    model.set_discharge_during_charge = model.set_charge_window = True
    model.set_reserve_enable = model.set_charge_freeze = False
    model.battery_charge_power_curve = model.battery_discharge_power_curve = {}
    model.prediction_kernel_enable = kernel
    if support is not None:
        model.plan_grid_charge_support_w = support
    pred = Prediction(model, forecasts.pv_forecast_minute_step, forecasts.pv_forecast_minute10_step, forecasts.load_minutes_step, forecasts.load_minutes_step10)
    return pred, model


def _run(pred, target=8, window=True, scenario=0):
    """Exercise the real public run method with one explicit charge request or ordinary demand."""
    limits = [target] if window else []
    windows = [{"start": pred.minutes_now, "end": pred.minutes_now + pred.forecast_minutes, "average": 10}] if window else []
    return pred.run_prediction(limits, windows, [], [], scenario, end_record=pred.forecast_minutes, save="test")


def _assert_step(pred, grid_kw, soc):
    """Check precise energy/cost and signed SOC, not merely an import upper bound."""
    assert abs(pred.plan_grid_support_import[0][0] - grid_kw * 1000) < 1e-6
    assert abs(pred.final_soc - soc) <= 0.000051
    assert abs(pred.final_metric - grid_kw / 12 * 10) <= 0.000051


def test_grid_support_defaults_and_kernel(base):
    """Default and explicit zero are identical; invalid opt-ins fail and kernels cannot omit physics."""
    default, _ = _prediction(base, support=None)
    disabled, _ = _prediction(base, support=0)
    assert _run(default) == _run(disabled)
    assert default.predict_soc == disabled.predict_soc and default.predict_grid_power == disabled.predict_grid_power
    assert default.plan_grid_support_import is None and disabled.plan_grid_support_import is None
    assert max(default.predict_grid_power.values()) > 9
    for value in (True, False, "6000", float("nan"), float("inf"), -1):
        try:
            _prediction(base, support=value)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid opt-in accepted")
    with patch("prediction.create_kernel_context", return_value=0) as build:
        pred, model = _prediction(base, kernel=True)
        assert model.prediction_kernel_enable and not pred.prediction_kernel_enable and pred.kernel_handle == 0
        build.assert_not_called()
    with patch("prediction.run_prediction_kernel", side_effect=AssertionError("Flagged kernel entered")):
        _run(pred)
    assert not getattr(base, "plan_grid_charge_support_w", 0)


def test_grid_support_signed_flow_and_loss(base):
    """Deferred charging is not added to the discharge; both conversion directions conserve energy."""
    pred, _ = _prediction(base, load=4.5)
    _run(pred)
    _assert_step(pred, 6, 5 + 1.5 / 12 * 0.96 * 0.97)
    pred, _ = _prediction(base)
    _run(pred)
    _assert_step(pred, 6, 5 - 0.52 / 12 / 0.96 / 0.97)
    assert pred.plan_grid_support_import[0][1] <= 6000 + 1e-6
    pred, _ = _prediction(base, charge=0)
    _run(pred)
    _assert_step(pred, 6, 5 - 0.52 / 12 / 0.96 / 0.97)


def test_grid_support_pv_and_source_limits(base):
    """PV serves load first, surplus can charge directly, and sources retain separate conversion caps."""
    for load, pv, hybrid, grid, delta in (
        (7.5, 1, True, 6, -0.54 / 12 / 0.96 / 0.97),
        (4.5, 1, True, 6, 2.46 / 12 * 0.96 * 0.97),
        (4.5, 2, True, 5.705, 3 / 12 * 0.97),
        (4.5, 2, False, 5.625, 3 / 12 * 0.97),
        (1, 5, True, 0, 3 / 12 * 0.97),
    ):
        pred, _ = _prediction(base, load=load, pv=pv, hybrid=hybrid)
        _run(pred)
        _assert_step(pred, grid, 5 + delta)
    pred, _ = _prediction(base, inverter=0.25)
    _run(pred)
    _assert_step(pred, 6.28, 5 - 0.25 / 12 / 0.97)
    pred, _ = _prediction(base, load=7.5, discharge=0.5, loss=1, battery_loss=1)
    _run(pred)
    _assert_step(pred, 7, 5 - 0.5 / 12)
    pred, _ = _prediction(base, load=1)
    _run(pred, scenario=1)
    _assert_step(pred, 1 + 3 * pred.charge_scaling10 / 0.96, 5 + 3 * pred.charge_scaling10 / 12 * 0.97)


def test_grid_support_soc_boundaries_and_reuse(base):
    """Reserve tails expose instantaneous overload; target saturation neither overcharges nor hides peaks."""
    pred, _ = _prediction(base, load=6.5, soc=1.1, support=5000)
    _run(pred)
    _assert_step(pred, 6.5 - 0.1 * 12 * 0.97 * 0.96, 1)
    assert pred.plan_grid_support_import[0][0] < 6000 < pred.plan_grid_support_import[0][1]
    pred, _ = _prediction(base, load=7.5, soc=1)
    _run(pred)
    _assert_step(pred, 7.5, 1)
    pred, _ = _prediction(base, load=4.5, soc=7.99)
    _run(pred)
    _assert_step(pred, 4.5 + 0.01 * 12 / 0.97 / 0.96, 8)
    assert abs(pred.plan_grid_support_import[0][1] - 6000) < 1e-6
    for soc, window in ((8, True), (5, False)):
        pred, _ = _prediction(base, soc=soc)
        other, _ = _prediction(base, soc=soc, support=0)
        assert _run(pred, window=window) == _run(other, window=window)
        assert pred.predict_grid_power == other.predict_grid_power
    pred, _ = _prediction(base)
    _run(pred)
    before = dict(pred.plan_grid_support_import)
    _run(pred, window=False)
    assert len(pred.plan_grid_support_import) == 1 and pred.plan_grid_support_import != before


def run_prediction_grid_support_tests(base):
    """Run non-vacuous native regressions through the registered test harness."""
    for test in (test_grid_support_defaults_and_kernel, test_grid_support_signed_flow_and_loss, test_grid_support_pv_and_source_limits, test_grid_support_soc_boundaries_and_reuse):
        test(base)
    return False
