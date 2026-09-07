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


def run_load_ml_rollout_tests(my_predbat=None):
    """Run the autoregressive rollout regression tests, returning a failure count."""
    failed = 0
    for test in (
        _test_rollout_diagnostic_scores_the_daily_pattern_baseline,
        _test_rate_forecast_running_out_does_not_zero_the_model_inputs,
        _test_holdout_scores_survive_a_save_load_roundtrip,
        _test_stats_sensor_exposes_the_holdout_scores,
    ):
        print("  Running {}...".format(test.__name__), end=" ")
        try:
            test()
            print("PASS")
        except Exception as error:  # pylint: disable=broad-except
            print("FAIL: {}".format(error))
            failed += 1
    return failed
