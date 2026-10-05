# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Regression tests for long-range degradation of the ML load forecast (#4673).

Issue #4673 reported a 48-hour forecast with spikes far above and below anything in
the history, for a household with ~10 kWh of free-rate load every day between 05:00
and 06:00. Replaying the reporter's saved model and history surfaced two defects.
Whether either one caused their report is unconfirmed - that needs their logs.

1. Missing forward exogenous data was silently zero-filled. Rates and temperature are
   3 of the 5 input channels (864 of 1446 features), and predict() substituted 0.0
   wherever the forward forecast was absent, putting 0 p/kWh and 0 degrees into a
   network trained on 33.6 p/kWh and 7 degrees. Measured on the reporter's own model,
   removing forward rate data moved the +8h forecast from 0.28 kWh MAE to 2.6-2.8 kWh.
   A healthy system does not hit this - rate_replicate() and publish_rates() cover the
   whole rollout - so it guards startup and failed-fetch cases rather than steady state.

2. No visibility of multi-step accuracy. The published mae_kwh is teacher-forced and
   stays small however badly the rollout behaves, so there was nothing to diagnose
   from. The rollout is now scored against the daily-pattern baseline predict()
   already blends in, and both figures are published. This is reporting only - it
   does not change the forecast.
"""

from datetime import datetime, timedelta, timezone

import numpy as np

from load_predictor import (
    HIDDEN_SIZES,
    LoadPredictor,
    NUM_EXPORT_RATE_FEATURES,
    NUM_IMPORT_RATE_FEATURES,
    NUM_LOAD_FEATURES,
    NUM_PV_FEATURES,
    NUM_TEMP_FEATURES,
    OUTPUT_STEPS,
    STEP_MINUTES,
    TOTAL_FEATURES,
)

# Synthetic household matching the shape reported in #4673
SPIKE_HOUR = 5  # Local hour of the free-rate window
SPIKE_KW = 11.0  # Load during the free hour
BASE_KW = 0.2  # Load the rest of the day
HISTORY_DAYS = 21


def _spike_profile_kwh(dt):
    """Per-5-min energy for a household with one large repeatable daily event."""
    kw = SPIKE_KW if dt.hour == SPIKE_HOUR else BASE_KW
    return kw * STEP_MINUTES / 60.0


def _spike_history(now_utc, days=HISTORY_DAYS):
    """Build {minute: kwh_per_step} history keyed by minutes back from now_utc."""
    return {minute: _spike_profile_kwh(now_utc - timedelta(minutes=minute)) for minute in range(0, days * 24 * 60, STEP_MINUTES)}


def _collapsed_predictor(constant_kwh_per_step):
    """
    Build a predictor whose network emits a constant, reproducing rollout collapse.

    Every weight is zero and every bias except the output bias is zero, so the forward
    pass returns the output bias regardless of its input. That is the degenerate state
    a lag-dominated model falls into once it is fed its own predictions, and it lets
    these tests exercise the real predict() path without training a model first.
    """
    predictor = LoadPredictor(log_func=lambda *args, **kwargs: None, max_load_kw=50.0)
    layer_sizes = [TOTAL_FEATURES] + HIDDEN_SIZES + [OUTPUT_STEPS]
    predictor.weights = [np.zeros((layer_sizes[i], layer_sizes[i + 1]), dtype=np.float32) for i in range(len(layer_sizes) - 1)]
    predictor.biases = [np.zeros(layer_sizes[i + 1], dtype=np.float32) for i in range(len(layer_sizes) - 1)]

    # Normalisation is the identity so the output bias *is* the predicted kWh per step
    predictor.feature_mean = np.zeros(TOTAL_FEATURES, dtype=np.float32)
    predictor.feature_std = np.ones(TOTAL_FEATURES, dtype=np.float32)
    predictor.target_mean = 0.0
    predictor.target_std = 1.0
    predictor.biases[-1][:] = constant_kwh_per_step

    # Adam state is not exercised here but save() serialises it
    predictor.m_weights = [np.zeros_like(w) for w in predictor.weights]
    predictor.v_weights = [np.zeros_like(w) for w in predictor.weights]
    predictor.m_biases = [np.zeros_like(b) for b in predictor.biases]
    predictor.v_biases = [np.zeros_like(b) for b in predictor.biases]

    predictor.model_initialized = True
    predictor.training_timestamp = datetime.now(timezone.utc)
    return predictor


def _test_rollout_diagnostic_scores_the_daily_pattern_baseline():
    """The rollout diagnostic reports the daily-pattern baseline alongside the model."""
    now_utc = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    history = _spike_history(now_utc)
    mean_energy = float(np.mean(list(history.values())))
    predictor = _collapsed_predictor(mean_energy)

    rollout_mae, _rollout_bias, pattern_mae = predictor._ar_rollout_diagnostic(history, now_utc, validation_holdout_hours=48)

    assert rollout_mae is not None, "Rollout diagnostic should score the model over the holdout"
    assert pattern_mae is not None, "Rollout diagnostic should also score the daily-pattern baseline"
    assert pattern_mae < rollout_mae, "Daily pattern should beat a collapsed rollout on a strongly repeatable profile, got pattern={:.4f} rollout={:.4f}".format(pattern_mae, rollout_mae)


def _test_rate_forecast_running_out_does_not_zero_the_model_inputs():
    """When the forward rate plan ends, the rollout must not fall back to 0 p/kWh.

    Rates are 576 of the 1446 input features. predbat's rates entity only reaches the end
    of tomorrow, so a 48-hour rollout always runs past it; substituting 0.0 there drags
    40% of the inputs far outside anything the model saw in training (#4673).
    """
    now_utc = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0)
    history = _spike_history(now_utc)
    # Flat tariff over the whole history, and no forward plan at all (negative keys absent)
    import_rates = {minute: 33.6 for minute in history}
    export_rates = {minute: 11.5 for minute in history}
    predictor = _collapsed_predictor(float(np.mean(list(history.values()))))

    captured = []
    original_normalize = predictor._normalize_features

    def _capture(features, **kwargs):
        """Record the raw feature row on its way into the network."""
        captured.append(np.asarray(features, dtype=np.float64).reshape(-1).copy())
        return original_normalize(features, **kwargs)

    predictor._normalize_features = _capture
    predictor.predict(history, now_utc, midnight_utc, import_rates=import_rates, export_rates=export_rates)

    import_offset = NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES
    export_offset = import_offset + NUM_IMPORT_RATE_FEATURES
    final_row = captured[-1]
    import_block = final_row[import_offset : import_offset + NUM_IMPORT_RATE_FEATURES]
    export_block = final_row[export_offset : export_offset + NUM_EXPORT_RATE_FEATURES]

    assert import_block.min() > 0.0, "Import rate features should never be zero-filled; {} of {} were zero at the end of the rollout".format(int((import_block == 0.0).sum()), len(import_block))
    assert export_block.min() > 0.0, "Export rate features should never be zero-filled; {} of {} were zero at the end of the rollout".format(int((export_block == 0.0).sum()), len(export_block))


def _test_holdout_scores_survive_a_save_load_roundtrip():
    """Saved models carry their holdout scores, so they survive a restart."""
    import os
    import tempfile

    predictor = _collapsed_predictor(0.05)
    predictor.rollout_mae = 0.0421
    predictor.pattern_mae = 0.0117

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "model.npz")
        assert predictor.save(path), "Model should save"

        reloaded = LoadPredictor(log_func=lambda *args, **kwargs: None, max_load_kw=50.0)
        assert reloaded.load(path), "Model should load"

    assert reloaded.rollout_mae == predictor.rollout_mae, "rollout_mae should survive the roundtrip, got {}".format(reloaded.rollout_mae)
    assert reloaded.pattern_mae == predictor.pattern_mae, "pattern_mae should survive the roundtrip, got {}".format(reloaded.pattern_mae)


def _test_stats_sensor_exposes_the_holdout_scores():
    """The stats sensor reports rollout and pattern error, not just teacher-forced MAE."""
    from load_ml_component import LoadMLComponent

    class MockBase:
        """Minimal PredBat stand-in for driving _publish_entity."""

        def __init__(self):
            """Set up the attributes the component reads."""
            self.prefix = "predbat"
            self.config_root = None
            self.now_utc = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
            self.midnight_utc = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
            self.minutes_now = 720
            self.local_tz = timezone.utc
            self.args = {}
            self.dashboard_calls = []

        def log(self, msg):
            """Swallow log output."""

        def get_arg(self, key, default=None, **kwargs):
            """Return the handful of settings the component asks for."""
            return {"load_today": ["sensor.load_today"], "load_power": None, "car_charging_energy": None}.get(key, default)

    mock_base = MockBase()
    component = LoadMLComponent(mock_base, load_ml_enable=True)
    component.dashboard_item = lambda entity_id, state, attributes, app: mock_base.dashboard_calls.append((entity_id, attributes))

    component.load_minutes_now = 10.5
    component.load_minutes_now_time = mock_base.now_utc
    component.current_predictions = {0: 0.1, 60: 1.3, 480: 9.7}
    component.predictor.validation_mae = 0.0065
    component.predictor.rollout_mae = 0.0421
    component.predictor.pattern_mae = 0.0117

    component._publish_entity()

    stats = dict(mock_base.dashboard_calls)["sensor.predbat_load_ml_stats"]
    assert stats.get("rollout_mae_kwh") == 0.0421, "Stats should report the rollout MAE, got {}".format(stats.get("rollout_mae_kwh"))
    assert stats.get("pattern_mae_kwh") == 0.0117, "Stats should report the daily-pattern MAE, got {}".format(stats.get("pattern_mae_kwh"))


def _test_configurable_forecast_preserves_shared_predictions():
    """Changing the horizon must preserve shared predictions and the original blend."""
    now = datetime(2026, 12, 30, 23, 55, tzinfo=timezone.utc)
    midnight = now.replace(hour=0, minute=0)
    history = _spike_history(now)
    predictor = _collapsed_predictor(0.05)
    baseline = predictor.predict(history, now, midnight)
    assert len(baseline) == 576, "Omitting forecast_hours must retain the 48-hour default"
    patterns = predictor._compute_daily_pattern(history, now)
    for requested, minutes in ((12, 1440), (24, 1440), (31, 1860), (31.47, 1860), (48, 2880), (48.01, 2880), (72, 4320), (96, 5760), (120, 7200), (48, 2880)):
        result = predictor.predict(history, now, midnight, forecast_hours=requested)
        assert list(result) == list(range(0, minutes, STEP_MINUTES)), "Wrong forecast coverage"
        shared = min(minutes, 2880)
        assert all(result[minute] == baseline[minute] for minute in range(0, shared, STEP_MINUTES)), "Shared forecasts changed with the horizon"
        increments = np.diff([0.0] + list(result.values()))
        assert np.all(np.isfinite(increments)) and np.all(increments >= 0), "Forecast must remain finite and cumulative"
        assert np.all(increments <= predictor.max_load_kw * STEP_MINUTES / 60.0 + 0.0001), "Forecast exceeds the physical load cap"
        for minute in range(48 * 60, minutes, STEP_MINUTES):
            target = now + timedelta(minutes=minute)
            slot = target.hour * 60 + target.minute
            expected = 0.5 * 0.05 + 0.5 * patterns[target.weekday()][slot]
            assert abs(increments[minute // STEP_MINUTES] - expected) < 0.00011, "Blend must remain 50/50 beyond 48 hours"


def _test_configurable_forecast_saved_model_and_input_fallback():
    """An existing model must support 96 hours without losing the input fallback."""
    import json
    import tempfile
    from pathlib import Path

    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    midnight = now.replace(hour=0, minute=0)
    history = _spike_history(now)
    predictor = _collapsed_predictor(0.05)
    with tempfile.TemporaryDirectory() as directory:
        checkpoint = str(Path(directory) / "model.npz")
        assert predictor.save(checkpoint)
        restored = LoadPredictor(log_func=lambda message: None)
        assert restored.load(checkpoint), "Default-horizon model should remain usable"
        messages = []
        restored.log = messages.append
        seen = []
        original_forward = restored._forward

        def capture(features, training=False):
            """Capture the newest exogenous features during the real rollout."""
            seen.append(features[0, [NUM_LOAD_FEATURES + NUM_PV_FEATURES, NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES, NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES]].copy())
            return original_forward(features, training=training)

        restored._forward = capture
        temperature = {minute: 18.0 for minute in range(-48 * 60, 24 * 60, STEP_MINUTES)}
        imports = {minute: 25.0 for minute in temperature}
        exports = {minute: 10.0 for minute in temperature}
        result = restored.predict(history, now, midnight, temp_minutes=temperature, import_rates=imports, export_rates=exports, forecast_hours=96)
        assert len(result) == 1152
        assert np.allclose(seen[-1], [18.0, 25.0, 10.0]), "Input forecasts must carry their last known values"
        assert any("1152 steps (96.0 hours)" in message for message in messages), "Rollout log must show the actual horizon"
        assert any("last known values carried forward" in message for message in messages), "Missing input coverage must still be logged"
        assert restored.save(checkpoint)
        with np.load(checkpoint) as saved:
            metadata = json.loads(str(saved["metadata_json"]))
        assert metadata["predict_horizon"] == 1152, "Saved metadata must reflect the last rollout"
        default_model = LoadPredictor(log_func=lambda message: None)
        assert default_model.load(checkpoint)
        assert len(default_model.predict(history, now, midnight)) == 576, "Saved horizon must not override the caller's default"


def _test_component_reads_current_forecast_hours():
    """The real component must read forecast_hours on every prediction request."""
    from load_ml_component import LoadMLComponent
    from types import SimpleNamespace

    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    settings = {"load_today": ["sensor.load_today"]}
    messages = []
    base = SimpleNamespace(prefix="predbat", config_root=None, now_utc=now, midnight_utc=now.replace(hour=0, minute=0), local_tz=timezone.utc, args=settings, log=messages.append)
    base.get_arg = lambda key, default=None, **kwargs: settings.get(key, default)
    component = LoadMLComponent(base, load_ml_enable=True)
    component.predictor = _collapsed_predictor(0.05)
    component.load_data = _spike_history(now)
    component.load_data_age_days = 21
    component.data_ready = True
    component.model_valid = True
    for requested, minutes in ((None, 2880), (96, 5760), (31, 1860), (31.47, 1860), (48.01, 2880), (72, 4320), (48, 2880), (12, 1440)):
        if requested is None:
            settings.pop("forecast_hours", None)
        else:
            settings["forecast_hours"] = requested
        hours = minutes / 60.0
        result = component._get_predictions(now, base.midnight_utc)
        assert len(result) == minutes // STEP_MINUTES, "Component did not follow the current setting"
        assert "over {:g}h".format(hours) in messages[-1], "Component log must show the actual duration"


def _test_component_future_inputs_outlast_short_history():
    """Available future temperatures and rates must not be clipped to load or PV history."""
    import asyncio
    from types import SimpleNamespace

    from const import TIME_FORMAT
    from load_ml_component import LoadMLComponent

    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

    async def check_coverage(forecast_hours, load_days, pv_days):
        """Exercise real input conversion with deliberately shorter sensor history."""
        settings = {"load_today": ["sensor.load_today"], "load_power": None, "pv_today": ["sensor.pv_today"] if pv_days else None, "car_charging_energy": None, "car_charging_hold": False, "forecast_hours": forecast_hours}
        base = SimpleNamespace(prefix="predbat", config_root=None, now_utc=now, midnight_utc=now.replace(hour=0), local_tz=timezone.utc, args=settings, log=lambda message: None)
        base.get_arg = lambda key, default=None, **kwargs: settings.get(key, default)
        source = {(now + timedelta(hours=hour)).strftime(TIME_FORMAT): 20.0 + hour / 100.0 for hour in range(-load_days * 24, forecast_hours + 25)}
        base.get_state_wrapper = lambda entity, default=None, attribute=None, **kwargs: source if attribute == "results" else default
        history_calls = []

        def load_history(reference_time, key, days, **kwargs):
            """Return cumulative sensor history with a known retention limit."""
            history_calls.append((key, days))
            age = pv_days if key == "pv_today" else load_days
            return {minute: (age * 1440 - minute) * 0.001 for minute in range(age * 1440, -1, -1)}, age

        rate_history_days = []

        def rate_history(days, reference_time, entity, **kwargs):
            """Record the historical rate request independently of future coverage."""
            rate_history_days.append(days)
            return {}

        base.minute_data_load = load_history
        base.minute_data_import_export = rate_history
        base.fetch_pv_forecast = lambda: ({}, {}, {})
        component = LoadMLComponent(base, load_ml_enable=True)
        result = await component._fetch_load_data()
        assert result[0] is not None, "Fetching load data failed"
        history_days = min(load_days, pv_days) if pv_days else load_days
        assert result[1] == history_days, "Future coverage must not change the reported history age"
        assert history_calls[0] == ("load_today", 28), "Load history request must retain its configured limit"
        if pv_days:
            assert history_calls[1] == ("pv_today", load_days), "PV history request must remain unchanged"
        assert rate_history_days == [history_days, history_days], "Historical rate requests must not grow with the forecast"
        for channel in result[4:]:
            assert all(-minute in channel for minute in range(STEP_MINUTES, forecast_hours * 60 + 1, STEP_MINUTES)), "Available future inputs were clipped to the history window"
            assert abs(channel[-forecast_hours * 60] - (20.0 + forecast_hours / 100.0)) < 0.0001, "The last input must be the supplied forecast value"

    for hours, load_days, pv_days in ((24, 2, None), (31, 1, None), (96, 2, None), (96, 7, 1), (168, 2, None), (412, 2, None), (48, 7, None)):
        asyncio.run(check_coverage(hours, load_days, pv_days))


def _test_load_ml_temperature_chart_uses_forecast_hours():
    """The real LoadMLPower chart must retain temperatures through its horizon."""
    from web import WebInterface
    from types import SimpleNamespace
    from unittest.mock import Mock

    now = datetime(2026, 12, 30, 23, 55, tzinfo=timezone.utc)
    settings = {}
    base = SimpleNamespace(now_utc=now, midnight_utc=now.replace(hour=0, minute=0), minutes_now=1435, plan_interval_minutes=30, soc_kwh_history={}, soc_kw=5.0)
    base.get_arg = lambda key, default=None, **kwargs: settings.get(key, default)
    view = WebInterface.__new__(WebInterface)
    view.base = base
    view.prefix = "predbat"
    temperatures = {(now + timedelta(minutes=minute)).isoformat(): 18.0 for minute in range(-1440, 10085, STEP_MINUTES)}

    def entity_results(entity):
        """Provide chart temperatures and a populated plan to pass the loading gate."""
        if entity == "sensor.predbat_temperature":
            return temperatures
        if entity == "predbat.soc_kw_best":
            return {now.isoformat(): 5.0}
        return {}

    view.get_entity_results = entity_results
    view.get_history_wrapper = lambda *args, **kwargs: []
    view.get_history_with_now_attrs = lambda *args, **kwargs: []
    view.render_chart = Mock(return_value="chart")
    for requested, minutes in ((None, 2880), (24, 1440), (31, 1860), (31.47, 1860), (48.01, 2880), (48, 2880), (72, 4320), (96, 5760), (120, 7200), (12, 1440)):
        if requested is None:
            settings.pop("forecast_hours", None)
        else:
            settings["forecast_hours"] = requested
        view.render_chart.reset_mock()
        view.get_chart("LoadMLPower")
        series = view.render_chart.call_args.args[0]
        data = next(item["data"] for item in series if item["name"] == "Temperature")
        assert (now - timedelta(hours=24)).isoformat() in data, "Temperature history should remain visible"
        # The chart groups points into 15-minute intervals; check the last visible bucket.
        last_bucket = minutes // 15 * 15
        assert (now + timedelta(minutes=last_bucket)).isoformat() in data, "Chart clipped temperatures before the configured horizon"
        assert (now + timedelta(minutes=last_bucket + 15)).isoformat() not in data, "Chart extended beyond the configured horizon"


def run_load_ml_rollout_tests(my_predbat=None):
    """Run the autoregressive rollout regression tests, returning a failure count."""
    failed = 0
    for test in (
        _test_rollout_diagnostic_scores_the_daily_pattern_baseline,
        _test_rate_forecast_running_out_does_not_zero_the_model_inputs,
        _test_holdout_scores_survive_a_save_load_roundtrip,
        _test_stats_sensor_exposes_the_holdout_scores,
        _test_configurable_forecast_preserves_shared_predictions,
        _test_configurable_forecast_saved_model_and_input_fallback,
        _test_component_reads_current_forecast_hours,
        _test_component_future_inputs_outlast_short_history,
        _test_load_ml_temperature_chart_uses_forecast_hours,
    ):
        print("  Running {}...".format(test.__name__), end=" ")
        try:
            test()
            print("PASS")
        except Exception as error:  # pylint: disable=broad-except
            print("FAIL: {}".format(error))
            failed += 1
    return failed
