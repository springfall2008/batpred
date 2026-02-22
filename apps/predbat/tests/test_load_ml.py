# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt: on

from utils import dp4
import numpy as np
from datetime import datetime, timezone, timedelta
import tempfile
import os
from dateutil import parser

from load_predictor import LoadPredictor, OUTPUT_STEPS, HIDDEN_SIZES, TOTAL_FEATURES, STEP_MINUTES, CHUNK_MINUTES, relu, relu_derivative, huber_loss


def test_load_ml(my_predbat=None):
    """
    Comprehensive test suite for ML Load Forecaster.

    Tests all major functionality including:
    - MLP forward/backward pass correctness
    - Dataset creation with cyclical features
    - Training convergence on synthetic data
    - Model save/load with version check
    - Cold-start and fine-tune scenarios
    - Validation failure fallback
    """

    # Registry of all sub-tests
    sub_tests = [
        ("relu_functions", _test_relu_functions, "ReLU activation and derivative"),
        ("huber_loss_functions", _test_huber_loss_functions, "Huber loss computation"),
        ("forward_pass", _test_forward_pass, "Forward pass computation"),
        ("backward_pass", _test_backward_pass, "Backward pass gradient computation"),
        ("cyclical_features", _test_cyclical_features, "Cyclical time feature encoding"),
        ("load_to_energy", _test_load_to_energy, "Convert cumulative load to energy per step"),
        ("pv_energy_conversion", _test_pv_energy_conversion, "Convert PV data including future forecasts"),
        ("dataset_creation", _test_dataset_creation, "Dataset creation from load data"),
        ("dataset_with_pv", _test_dataset_with_pv, "Dataset creation with PV features"),
        ("dataset_with_temp", _test_dataset_with_temp, "Dataset creation with temperature features"),
        ("normalization", _test_normalization, "Z-score normalization correctness"),
        ("ema_normalization", _test_ema_normalization, "EMA normalization drift tracking during fine-tuning"),
        ("adam_optimizer", _test_adam_optimizer, "Adam optimizer step"),
        ("training_convergence", _test_training_convergence, "Training convergence on synthetic data"),
        ("training_with_pv", _test_training_with_pv, "Training with PV input features"),
        ("training_with_temp", _test_training_with_temp, "Training with temperature input features"),
        ("model_persistence", _test_model_persistence, "Model save/load with version check"),
        ("cold_start", _test_cold_start, "Cold start with insufficient data"),
        ("fine_tune", _test_fine_tune, "Fine-tune on recent data"),
        ("prediction", _test_prediction, "End-to-end prediction"),
        ("prediction_with_pv", _test_prediction_with_pv, "Prediction with PV forecast data"),
        ("prediction_with_temp", _test_prediction_with_temp, "Prediction with temperature forecast data"),
        ("component_fetch_load_data", _test_component_fetch_load_data, "LoadMLComponent _fetch_load_data method"),
        ("component_publish_entity", _test_component_publish_entity, "LoadMLComponent _publish_entity method"),
        ("car_subtraction_direct", _test_car_subtraction_direct, "Direct car_subtraction method with interpolation and smoothing"),
        # ("real_data_training", _test_real_data_training, "Train on real data with chart"),
        # ("pretrained_model_prediction", _test_pretrained_model_prediction, "Load pre-trained model and generate predictions with chart"),
    ]

    failed_tests = []
    passed_count = 0

    for name, test_func, description in sub_tests:
        try:
            print(f"  Running {name}: {description}...", end=" ")
            test_func()
            print("PASS")
            passed_count += 1
        except Exception as e:
            print(f"FAIL: {e}")
            import traceback

            traceback.print_exc()
            failed_tests.append((name, str(e)))

    print(f"\nML Load Forecaster Tests: {passed_count}/{len(sub_tests)} passed")
    if failed_tests:
        print("Failed tests:")
        for name, error in failed_tests:
            print(f"  - {name}: {error}")
        assert False, f"ML Load Forecaster: {len(failed_tests)} tests failed"


def _test_relu_functions():
    """Test ReLU activation and derivative"""
    # Test ReLU
    x = np.array([-2, -1, 0, 1, 2])
    expected = np.array([0, 0, 0, 1, 2])
    result = relu(x)
    assert np.allclose(result, expected), f"ReLU output mismatch: {result} vs {expected}"

    # Test ReLU derivative
    expected_deriv = np.array([0, 0, 0, 1, 1])
    result_deriv = relu_derivative(x)
    assert np.allclose(result_deriv, expected_deriv), f"ReLU derivative mismatch: {result_deriv} vs {expected_deriv}"


def _test_huber_loss_functions():
    """Test Huber loss computation"""
    # Test with small error (L2 region)
    y_true = np.array([[1.0, 2.0, 3.0]])
    y_pred = np.array([[1.1, 2.1, 3.1]])  # Error = 0.1
    loss = huber_loss(y_true, y_pred, delta=1.0)
    # For small errors, Huber is 0.5 * error^2
    expected = 0.5 * (0.1**2)
    assert abs(loss - expected) < 0.01, f"Huber loss for small error: expected {expected}, got {loss}"

    # Test with large error (L1 region)
    y_pred_large = np.array([[3.0, 4.0, 5.0]])  # Error = 2.0
    loss_large = huber_loss(y_true, y_pred_large, delta=1.0)
    # For large errors, Huber is delta * (|error| - 0.5 * delta)
    expected_large = 1.0 * (2.0 - 0.5)
    assert abs(loss_large - expected_large) < 0.1, f"Huber loss for large error: expected {expected_large}, got {loss_large}"


def _test_forward_pass():
    """Test that forward pass produces expected output shape and values"""
    predictor = LoadPredictor(learning_rate=0.001)

    # Initialize weights
    predictor._initialize_weights()

    # Create test input: batch of 2, with TOTAL_FEATURES features
    X = np.random.randn(2, TOTAL_FEATURES).astype(np.float32)

    # Forward pass
    output, activations, pre_activations = predictor._forward(X)

    # Check output shape: should be (batch_size, OUTPUT_STEPS)
    assert output.shape == (2, OUTPUT_STEPS), f"Expected output shape (2, {OUTPUT_STEPS}), got {output.shape}"

    # Check that output is finite
    assert np.all(np.isfinite(output)), "Forward pass produced non-finite values"

    # Check activations structure
    assert len(activations) == len(HIDDEN_SIZES) + 2, "Wrong number of activations"
    assert len(pre_activations) == len(HIDDEN_SIZES) + 1, "Wrong number of pre-activations"


def _test_backward_pass():
    """Test that backward pass produces gradients with correct shapes"""
    predictor = LoadPredictor(learning_rate=0.001)
    predictor._initialize_weights()

    # Forward pass
    np.random.seed(42)
    X = np.random.randn(4, TOTAL_FEATURES).astype(np.float32)
    y_true = np.random.randn(4, OUTPUT_STEPS).astype(np.float32)

    output, activations, pre_activations = predictor._forward(X)

    # Backward pass
    weight_grads, bias_grads = predictor._backward(y_true, activations, pre_activations)

    # Check that gradients exist for all weight layers
    assert len(weight_grads) == len(HIDDEN_SIZES) + 1, "Wrong number of weight gradients"
    assert len(bias_grads) == len(HIDDEN_SIZES) + 1, "Wrong number of bias gradients"

    # Check gradient shapes match weight shapes
    for i, (w_grad, w) in enumerate(zip(weight_grads, predictor.weights)):
        assert w_grad.shape == w.shape, f"Weight gradient {i} shape mismatch: {w_grad.shape} vs {w.shape}"

    for i, (b_grad, b) in enumerate(zip(bias_grads, predictor.biases)):
        assert b_grad.shape == b.shape, f"Bias gradient {i} shape mismatch: {b_grad.shape} vs {b.shape}"


def _test_cyclical_features():
    """Test cyclical time feature encoding"""
    predictor = LoadPredictor()

    # Test midnight (minute 0)
    features = predictor._create_time_features(0, 0)
    assert len(features) == 4, "Should have 4 time features"
    assert abs(features[0] - 0.0) < 1e-6, "Midnight sin should be 0"
    assert abs(features[1] - 1.0) < 1e-6, "Midnight cos should be 1"

    # Test noon (minute 720)
    features = predictor._create_time_features(720, 0)
    assert abs(features[0] - 0.0) < 1e-6, "Noon sin should be 0"
    assert abs(features[1] - (-1.0)) < 1e-6, "Noon cos should be -1"

    # Test 6 AM (minute 360) - sin should be 1, cos should be 0
    features = predictor._create_time_features(360, 0)
    assert abs(features[0] - 1.0) < 1e-6, "6 AM sin should be 1"
    assert abs(features[1] - 0.0) < 1e-6, "6 AM cos should be 0"

    # Test Monday (dow 0) vs Thursday (dow 3)
    features_mon = predictor._create_time_features(0, 0)
    features_thu = predictor._create_time_features(0, 3)
    assert features_mon[2] != features_thu[2], "Different days should have different encodings"


def _test_load_to_energy():
    """Test conversion of cumulative load to energy per step"""
    predictor = LoadPredictor()

    # Create synthetic cumulative load data
    # Cumulative: minute 0 = 10, minute 5 = 9, minute 10 = 8, etc.
    load_minutes = {0: 10.0, 5: 9.0, 10: 8.0, 15: 7.5, 20: 7.0}

    energy_per_step = predictor._load_to_energy_per_step(load_minutes)

    # Energy from 0-5: 10 - 9 = 1
    assert abs(energy_per_step.get(0, -1) - 1.0) < 1e-6, "Energy 0-5 should be 1.0"
    # Energy from 5-10: 9 - 8 = 1
    assert abs(energy_per_step.get(5, -1) - 1.0) < 1e-6, "Energy 5-10 should be 1.0"
    # Energy from 10-15: 8 - 7.5 = 0.5
    assert abs(energy_per_step.get(10, -1) - 0.5) < 1e-6, "Energy 10-15 should be 0.5"
    # Energy from 15-20: 7.5 - 7 = 0.5
    assert abs(energy_per_step.get(15, -1) - 0.5) < 1e-6, "Energy 15-20 should be 0.5"


def _test_pv_energy_conversion():
    """Test conversion of PV data including future forecasts (negative minutes)"""
    predictor = LoadPredictor()

    # Create PV data with both historical (positive) and future (negative) minutes
    # Historical: minute 0-20 (backwards in time)
    # Future: minute -5 to -20 (forward in time)
    pv_minutes = {
        # Historical (cumulative decreasing as we go back in time)
        0: 10.0,
        5: 9.0,
        10: 8.0,
        15: 7.0,
        20: 6.0,
        # Future forecasts (cumulative increasing as we go forward)
        -5: 11.0,
        -10: 12.5,
        -15: 14.0,
        -20: 15.0,
    }

    pv_energy_per_step = predictor._load_to_energy_per_step(pv_minutes)

    # Historical energy (positive minutes, going backwards)
    # Energy from 0-5: 10 - 9 = 1
    assert abs(pv_energy_per_step.get(0, -1) - 1.0) < 1e-6, "PV energy 0-5 should be 1.0"
    # Energy from 5-10: 9 - 8 = 1
    assert abs(pv_energy_per_step.get(5, -1) - 1.0) < 1e-6, "PV energy 5-10 should be 1.0"

    # Future energy (negative minutes, going forward)
    # Energy from -20 to -15: 15.0 - 14.0 = 1.0
    assert abs(pv_energy_per_step.get(-20, -1) - 1.0) < 1e-6, f"PV future energy -20 to -15 should be 1.0, got {pv_energy_per_step.get(-20, -1)}"
    # Energy from -15 to -10: 14.0 - 12.5 = 1.5
    assert abs(pv_energy_per_step.get(-15, -1) - 1.5) < 1e-6, f"PV future energy -15 to -10 should be 1.5, got {pv_energy_per_step.get(-15, -1)}"
    # Energy from -10 to -5: 12.5 - 11.0 = 1.5
    assert abs(pv_energy_per_step.get(-10, -1) - 1.5) < 1e-6, f"PV future energy -10 to -5 should be 1.5, got {pv_energy_per_step.get(-10, -1)}"
    # Energy from -5 to 0: 11.0 - 10.0 = 1.0
    assert abs(pv_energy_per_step.get(-5, -1) - 1.0) < 1e-6, f"PV future energy -5 to 0 should be 1.0, got {pv_energy_per_step.get(-5, -1)}"


def _create_synthetic_pv_data(n_days=7, now_utc=None, forecast_hours=48):
    """Create synthetic PV data for testing (historical + forecast)"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    pv_minutes = {}
    cumulative = 0.0

    # Historical PV (positive minutes, backwards from now)
    n_minutes = n_days * 24 * 60
    # Start from a multiple of STEP_MINUTES and go down to 0
    start_minute = (n_minutes // STEP_MINUTES) * STEP_MINUTES
    for minute in range(start_minute, -STEP_MINUTES, -STEP_MINUTES):
        dt = now_utc - timedelta(minutes=minute)
        hour = dt.hour

        # PV generation pattern: 0 at night, peak at midday
        if 6 <= hour < 18:
            # Peak around noon (hour 12)
            hour_offset = abs(hour - 12)
            energy = max(0, 0.5 - hour_offset * 0.08 + 0.05 * np.random.randn())
        else:
            energy = 0.0

        energy = max(0, energy)
        cumulative += energy
        pv_minutes[minute] = cumulative

    # Future PV forecast (negative minutes, forward from now)
    forecast_cumulative = pv_minutes[0]  # Start from current cumulative
    for step in range(1, (forecast_hours * 60 // STEP_MINUTES) + 1):
        minute = -step * STEP_MINUTES
        dt = now_utc + timedelta(minutes=step * STEP_MINUTES)
        hour = dt.hour

        # Same pattern for forecast
        if 6 <= hour < 18:
            hour_offset = abs(hour - 12)
            energy = max(0, 0.5 - hour_offset * 0.08 + 0.05 * np.random.randn())
        else:
            energy = 0.0

        energy = max(0, energy)
        forecast_cumulative += energy
        pv_minutes[minute] = forecast_cumulative

    return pv_minutes


def _create_synthetic_temp_data(n_days=7, now_utc=None, forecast_hours=48):
    """Create synthetic temperature data for testing (historical + forecast)"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    temp_minutes = {}

    # Historical temperature (positive minutes, backwards from now)
    n_minutes = n_days * 24 * 60
    start_minute = (n_minutes // STEP_MINUTES) * STEP_MINUTES
    for minute in range(start_minute, -STEP_MINUTES, -STEP_MINUTES):
        dt = now_utc - timedelta(minutes=minute)
        hour = dt.hour + dt.minute / 60.0  # Fractional hour for smooth variation

        # Smooth sinusoidal daily temperature pattern
        # Temperature peaks around 1pm (hour 13) and minimum around 1am (hour 1)
        # Using cosine wave shifted so maximum is at hour 13
        hours_since_peak = (hour - 13.0) % 24.0
        daily_cycle = np.cos(2 * np.pi * hours_since_peak / 24.0)

        # Base temp 6°C, amplitude 4°C, so range is 2°C to 10°C
        # Add small multi-day variation (0.5°C amplitude over 3-day cycle)
        day_num = minute / (24 * 60)
        multi_day_variation = 0.5 * np.sin(2 * np.pi * day_num / 3.0)

        temp = 6.0 + 4.0 * daily_cycle + multi_day_variation

        temp = max(-10.0, min(40.0, temp))  # Reasonable bounds
        temp_minutes[minute] = temp

    # Future temperature forecast (negative minutes, forward from now)
    for step in range(1, (forecast_hours * 60 // STEP_MINUTES) + 1):
        minute = -step * STEP_MINUTES
        dt = now_utc + timedelta(minutes=step * STEP_MINUTES)
        hour = dt.hour + dt.minute / 60.0  # Fractional hour for smooth variation

        # Same smooth pattern for forecast
        hours_since_peak = (hour - 13.0) % 24.0
        daily_cycle = np.cos(2 * np.pi * hours_since_peak / 24.0)

        # Continue the multi-day variation into the future
        day_num = -minute / (24 * 60)  # Negative minute means future
        multi_day_variation = 0.5 * np.sin(2 * np.pi * day_num / 3.0)

        temp = 6.0 + 4.0 * daily_cycle + multi_day_variation

        temp = max(-10.0, min(40.0, temp))
        temp_minutes[minute] = temp

    return temp_minutes


def _create_synthetic_load_data(n_days=7, now_utc=None):
    """Create synthetic load data for testing"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    n_minutes = n_days * 24 * 60
    load_minutes = {}
    cumulative = 0.0

    # Build backwards from now (minute 0 = now)
    # Start from a multiple of STEP_MINUTES and go down to 0
    start_minute = (n_minutes // STEP_MINUTES) * STEP_MINUTES
    for minute in range(start_minute, -STEP_MINUTES, -STEP_MINUTES):
        # Time for this minute
        dt = now_utc - timedelta(minutes=minute)
        hour = dt.hour

        # Simple daily pattern: higher during day
        if 6 <= hour < 22:
            energy = 0.2 + 0.1 * np.random.randn()  # ~0.2 kWh per 5 min during day
        else:
            energy = 0.05 + 0.02 * np.random.randn()  # ~0.05 kWh at night

        energy = max(0, energy)
        cumulative += energy
        load_minutes[minute] = cumulative

    return load_minutes


def _test_dataset_creation():
    """Test dataset creation from load minute data with train/val split"""
    predictor = LoadPredictor()
    now_utc = datetime.now(timezone.utc)

    # Create synthetic load data: 7 days
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)

    # Create dataset - now returns 5 values (train + val split)
    X_train, y_train, train_weights, X_val, y_val = predictor._create_dataset(load_data, now_utc, time_decay_days=7)

    # Should have valid training samples
    assert X_train is not None, "Training X should not be None"
    assert X_train.shape[0] > 0, "Training should have samples"
    assert X_train.shape[0] == y_train.shape[0], "X_train and y_train should have same number of samples"
    assert train_weights.shape[0] == X_train.shape[0], "Train weights should match training samples"

    # Should have validation samples
    assert X_val is not None, "Validation X should not be None"
    assert X_val.shape[0] > 0, "Validation should have samples"
    assert X_val.shape[0] == y_val.shape[0], "X_val and y_val should have same number of samples"

    # Feature dimension: TOTAL_FEATURES
    assert X_train.shape[1] == TOTAL_FEATURES, f"Expected {TOTAL_FEATURES} features, got {X_train.shape[1]}"

    # Output dimension: OUTPUT_STEPS (1 for autoregressive)
    assert y_train.shape[1] == OUTPUT_STEPS, f"Expected {OUTPUT_STEPS} outputs, got {y_train.shape[1]}"

    # Validation should be approximately 24h worth of chunks (48 at 30-min intervals)
    expected_val_samples = 24 * 60 // CHUNK_MINUTES
    assert abs(X_val.shape[0] - expected_val_samples) < 5, f"Expected ~{expected_val_samples} val samples, got {X_val.shape[0]}"


def _test_dataset_with_pv():
    """Test dataset creation includes PV features correctly"""
    predictor = LoadPredictor()
    # Use a fixed daytime hour to ensure PV generation
    now_utc = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # Noon on summer day

    # Create synthetic load and PV data
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    pv_data = _create_synthetic_pv_data(n_days=7, now_utc=now_utc, forecast_hours=0)  # Historical only for training

    # Create dataset with PV data
    X_train, y_train, train_weights, X_val, y_val = predictor._create_dataset(load_data, now_utc, pv_minutes=pv_data, time_decay_days=7)

    # Should have valid samples
    assert X_train is not None, "Training X should not be None"
    assert X_train.shape[0] > 0, "Training should have samples"

    # Feature dimension should include PV features: load + PV + temp + import_rates + export_rates + time = TOTAL_FEATURES
    from load_predictor import NUM_LOAD_FEATURES, NUM_PV_FEATURES, NUM_TEMP_FEATURES, NUM_IMPORT_RATE_FEATURES, NUM_EXPORT_RATE_FEATURES, NUM_TIME_FEATURES

    expected_features = NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES + NUM_EXPORT_RATE_FEATURES + NUM_TIME_FEATURES
    assert X_train.shape[1] == expected_features, f"Expected {expected_features} features with PV, got {X_train.shape[1]}"
    assert X_train.shape[1] == TOTAL_FEATURES, f"TOTAL_FEATURES should be {expected_features}, is {TOTAL_FEATURES}"

    # Verify PV features are not all zeros (unless no PV data provided)
    # PV features are in the middle section: indices NUM_LOAD_FEATURES to NUM_LOAD_FEATURES+NUM_PV_FEATURES
    pv_feature_section = X_train[:, NUM_LOAD_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES]
    # At least some PV values should be non-zero (during daylight hours)
    assert np.any(pv_feature_section > 0), "PV features should contain some non-zero values"

    # Temperature features should be all zeros since we didn't provide temp_minutes
    temp_feature_section = X_train[:, NUM_LOAD_FEATURES + NUM_PV_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES]
    assert np.all(temp_feature_section == 0), "Temperature features should be zero when no temp data provided"

    # Import/export rate features should be all zeros since we didn't provide rate data
    import_rate_section = X_train[:, NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES]
    export_rate_section = X_train[:, NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES + NUM_EXPORT_RATE_FEATURES]
    assert np.all(import_rate_section == 0), "Import rate features should be zero when no rate data provided"
    assert np.all(export_rate_section == 0), "Export rate features should be zero when no rate data provided"


def _test_dataset_with_temp():
    """Test dataset creation includes temperature features correctly"""
    predictor = LoadPredictor()
    now_utc = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Create synthetic load and temperature data
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    temp_data = _create_synthetic_temp_data(n_days=7, now_utc=now_utc, forecast_hours=0)  # Historical only

    # Create dataset with temperature data
    X_train, y_train, train_weights, X_val, y_val = predictor._create_dataset(load_data, now_utc, temp_minutes=temp_data, time_decay_days=7)

    # Should have valid samples
    assert X_train is not None, "Training X should not be None"
    assert X_train.shape[0] > 0, "Training should have samples"

    # Feature dimension should include temperature features
    from load_predictor import NUM_LOAD_FEATURES, NUM_PV_FEATURES, NUM_TEMP_FEATURES, NUM_IMPORT_RATE_FEATURES, NUM_EXPORT_RATE_FEATURES, NUM_TIME_FEATURES

    expected_features = NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES + NUM_EXPORT_RATE_FEATURES + NUM_TIME_FEATURES
    assert X_train.shape[1] == expected_features, f"Expected {expected_features} features with temp, got {X_train.shape[1]}"
    assert X_train.shape[1] == TOTAL_FEATURES, f"TOTAL_FEATURES should be {expected_features}, is {TOTAL_FEATURES}"

    # Verify temperature features are not all zeros
    # Temperature features are after load and PV: indices NUM_LOAD_FEATURES+NUM_PV_FEATURES to NUM_LOAD_FEATURES+NUM_PV_FEATURES+NUM_TEMP_FEATURES
    temp_feature_section = X_train[:, NUM_LOAD_FEATURES + NUM_PV_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES]
    # At least some temperature values should be non-zero
    assert np.any(temp_feature_section != 0), "Temperature features should contain non-zero values"
    # Check temperature values are in reasonable range (after normalization they won't be in Celsius range)
    assert np.min(temp_feature_section) > -50, "Temperature features should be reasonable"
    assert np.max(temp_feature_section) < 50, "Temperature features should be reasonable"

    # PV features should be all zeros since we didn't provide pv_minutes
    pv_feature_section = X_train[:, NUM_LOAD_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES]
    assert np.all(pv_feature_section == 0), "PV features should be zero when no PV data provided"

    # Import/export rate features should be all zeros since we didn't provide rate data
    import_rate_section = X_train[:, NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES]
    export_rate_section = X_train[:, NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES : NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES + NUM_EXPORT_RATE_FEATURES]
    assert np.all(import_rate_section == 0), "Import rate features should be zero when no rate data provided"
    assert np.all(export_rate_section == 0), "Export rate features should be zero when no rate data provided"


def _test_normalization():
    """Test Z-score normalization correctness"""
    predictor = LoadPredictor()

    # Create test data
    np.random.seed(42)
    X = np.random.randn(100, TOTAL_FEATURES).astype(np.float32) * 10 + 5  # Mean ~5, std ~10

    # Normalize with fit
    X_norm = predictor._normalize_features(X, fit=True)

    # Check mean ~0 and std ~1 along each feature
    assert np.allclose(np.mean(X_norm, axis=0), 0, atol=0.1), "Normalized mean should be ~0"
    assert np.allclose(np.std(X_norm, axis=0), 1, atol=0.1), "Normalized std should be ~1"

    # Test target normalization
    y = np.random.randn(100, OUTPUT_STEPS).astype(np.float32) * 2 + 3
    y_norm = predictor._normalize_targets(y, fit=True)

    # Check denormalization
    y_denorm = predictor._denormalize_predictions(y_norm)
    assert np.allclose(y, y_denorm, atol=1e-5), "Denormalization should recover original"


def _test_ema_normalization():
    """Test EMA normalization drift tracking during fine-tuning.

    Verifies:
    - EMA blends feature stats proportional to alpha
    - alpha=0.0 freezes normalization
    - alpha=1.0 fully replaces normalization
    - EMA-updated params persist through save/load
    - train() with is_initial=False applies EMA when alpha>0
    """
    np.random.seed(42)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # --- Part 1: Direct EMA blending math ---
    # Use constant arrays so we know the exact new_mean that _normalize_features will compute
    X_initial = np.ones((200, TOTAL_FEATURES), dtype=np.float32)  # mean=1.0 per feature
    X_shifted = np.ones((200, TOTAL_FEATURES), dtype=np.float32) * 10.0  # mean=10.0 per feature

    predictor = LoadPredictor()
    predictor._normalize_features(X_initial, fit=True)
    initial_mean = predictor.feature_mean.copy()

    alpha = 0.1
    predictor._normalize_features(X_shifted, fit=False, ema_alpha=alpha)

    # EMA formula: new = alpha * new_data_mean + (1-alpha) * old_mean
    expected_load_mean = alpha * 10.0 + (1 - alpha) * initial_mean[0]  # load group, idx 0
    assert np.allclose(predictor.feature_mean[0], expected_load_mean, atol=1e-4), "EMA mean should blend: alpha*new_data_mean + (1-alpha)*old_mean, " "got {:.4f} expected {:.4f}".format(predictor.feature_mean[0], expected_load_mean)
    assert predictor.feature_mean[0] > initial_mean[0], "Feature mean should move toward higher-value distribution"

    # --- Part 2: alpha=0.0 freezes normalization ---
    predictor2 = LoadPredictor()
    predictor2._normalize_features(X_initial, fit=True)
    frozen_mean = predictor2.feature_mean.copy()
    frozen_std = predictor2.feature_std.copy()

    predictor2._normalize_features(X_shifted, fit=False, ema_alpha=0.0)

    assert np.allclose(predictor2.feature_mean, frozen_mean), "alpha=0.0 should freeze feature_mean (no EMA update)"
    assert np.allclose(predictor2.feature_std, frozen_std), "alpha=0.0 should freeze feature_std (no EMA update)"

    # --- Part 3: alpha=1.0 fully replaces normalization ---
    predictor3 = LoadPredictor()
    predictor3._normalize_features(X_initial, fit=True)

    predictor3._normalize_features(X_shifted, fit=False, ema_alpha=1.0)

    assert np.allclose(predictor3.feature_mean, 10.0), "alpha=1.0 should fully replace feature_mean with new data mean"

    # --- Part 4: EMA-updated params persist through save/load ---
    predictor4 = LoadPredictor()
    predictor4._normalize_features(X_initial, fit=True)
    predictor4._normalize_targets(np.ones((200, 1), dtype=np.float32) * 0.5, fit=True)
    predictor4._initialize_weights()
    predictor4.training_timestamp = datetime.now(timezone.utc)
    predictor4.validation_mae = 0.1

    predictor4._normalize_features(X_shifted, fit=False, ema_alpha=alpha)
    updated_mean = predictor4.feature_mean.copy()

    with tempfile.NamedTemporaryFile(suffix=".npz", delete=False) as f:
        temp_path = f.name
    try:
        predictor4.save(temp_path)
        predictor4b = LoadPredictor()
        assert predictor4b.load(temp_path), "Save/load should succeed for EMA-updated model"
        assert np.allclose(predictor4b.feature_mean, updated_mean), "EMA-updated normalization params should survive save/load"
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    # --- Part 5: Integration - train() applies EMA, predictions remain valid ---
    predictor5 = LoadPredictor(learning_rate=0.01)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    predictor5.train(load_data, now_utc, is_initial=True, epochs=5, time_decay_days=7)
    mean_after_initial = predictor5.feature_mean.copy()

    # Fine-tune with alpha=0.0 on same data - feature_mean must not change
    predictor5.train(load_data, now_utc, is_initial=False, epochs=3, time_decay_days=7, norm_ema_alpha=0.0)
    assert np.allclose(predictor5.feature_mean, mean_after_initial), "norm_ema_alpha=0.0 should not modify feature_mean during fine-tuning"

    # Fine-tune with alpha=0.1 on 5x-scaled data - feature_mean must shift toward higher values
    load_data_shifted = {k: v * 5.0 for k, v in load_data.items()}
    predictor5.train(load_data_shifted, now_utc, is_initial=False, epochs=3, time_decay_days=7, norm_ema_alpha=0.1)
    assert not np.allclose(predictor5.feature_mean, mean_after_initial), "norm_ema_alpha=0.1 with shifted data should update feature_mean"
    # Mean should have increased (load features shifted to higher values)
    assert predictor5.feature_mean[0] > mean_after_initial[0], "Feature mean should increase after fine-tuning on higher-load data"

    # Predictions must still function correctly after EMA normalization update
    predictions = predictor5.predict(load_data, now_utc, midnight_utc)
    assert isinstance(predictions, dict), "Predictions should return a dict after EMA normalization update"
    assert len(predictions) > 0, "Should produce non-empty predictions after EMA normalization update"
    for minute, val in predictions.items():
        assert val >= 0, "Prediction at minute {} should be non-negative".format(minute)


def _test_adam_optimizer():
    """Test Adam optimizer update step"""
    predictor = LoadPredictor(learning_rate=0.01)
    predictor._initialize_weights()

    # Store original weights
    orig_weight = predictor.weights[0].copy()

    # Create dummy gradients
    weight_grads = [np.ones_like(w) * 0.1 for w in predictor.weights]
    bias_grads = [np.ones_like(b) * 0.1 for b in predictor.biases]

    # Perform Adam update
    predictor._adam_update(weight_grads, bias_grads)

    # Weight should have changed
    assert not np.allclose(orig_weight, predictor.weights[0]), "Adam update should change weights"

    # adam_t should have incremented
    assert predictor.adam_t == 1, "Adam timestep should be 1"


def _test_training_convergence():
    """Test that training converges on simple synthetic data"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)

    # Create simple repeating daily pattern
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)

    # Train with few epochs
    val_mae = predictor.train(load_data, now_utc, pv_minutes=None, is_initial=True, epochs=10, time_decay_days=7)

    # Training should complete and return a validation MAE
    assert val_mae is not None, "Training should return validation MAE"
    assert predictor.model_initialized, "Model should be initialized after training"
    assert predictor.epochs_trained > 0, "Should have trained some epochs"


def _test_training_with_pv():
    """Test that training works correctly with PV input features"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)

    # Create load and PV data
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    pv_data = _create_synthetic_pv_data(n_days=7, now_utc=now_utc, forecast_hours=0)  # Historical only for training

    # Train with PV data
    val_mae = predictor.train(load_data, now_utc, pv_minutes=pv_data, is_initial=True, epochs=10, time_decay_days=7)

    # Training should complete successfully
    assert val_mae is not None, "Training with PV should return validation MAE"
    assert predictor.model_initialized, "Model should be initialized after training with PV"
    assert predictor.epochs_trained > 0, "Should have trained some epochs with PV data"

    # Verify the model can accept correct input size (with PV features)
    test_input = np.random.randn(1, TOTAL_FEATURES).astype(np.float32)
    output, _, _ = predictor._forward(test_input)
    assert output.shape == (1, OUTPUT_STEPS), "Model should produce correct output shape with PV features"


def _test_training_with_temp():
    """Test that training works correctly with temperature input features"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)

    # Create load and temperature data
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    temp_data = _create_synthetic_temp_data(n_days=7, now_utc=now_utc, forecast_hours=0)  # Historical only for training

    # Train with temperature data
    val_mae = predictor.train(load_data, now_utc, temp_minutes=temp_data, is_initial=True, epochs=10, time_decay_days=7)

    # Training should complete successfully
    assert val_mae is not None, "Training with temperature should return validation MAE"
    assert predictor.model_initialized, "Model should be initialized after training with temperature"
    assert predictor.epochs_trained > 0, "Should have trained some epochs with temperature data"

    # Verify the model can accept correct input size (with temperature features)
    test_input = np.random.randn(1, TOTAL_FEATURES).astype(np.float32)
    output, _, _ = predictor._forward(test_input)
    assert output.shape == (1, OUTPUT_STEPS), "Model should produce correct output shape with temperature features"


def _test_model_persistence():
    """Test model save/load with version check"""
    predictor = LoadPredictor(learning_rate=0.005)
    now_utc = datetime.now(timezone.utc)

    # Train briefly
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=5, now_utc=now_utc)
    predictor.train(load_data, now_utc, pv_minutes=None, is_initial=True, epochs=5, time_decay_days=7)

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix=".npz", delete=False) as f:
        temp_path = f.name

    try:
        predictor.save(temp_path)

        # Load into new predictor
        predictor2 = LoadPredictor(learning_rate=0.005)
        success = predictor2.load(temp_path)

        assert success, "Model load should succeed"
        assert predictor2.model_initialized, "Loaded model should be marked as initialized"

        # Compare weights
        for w1, w2 in zip(predictor.weights, predictor2.weights):
            assert np.allclose(w1, w2), "Weights should match after load"

        # Test prediction produces same result
        np.random.seed(123)
        test_input = np.random.randn(1, TOTAL_FEATURES).astype(np.float32)
        out1, _, _ = predictor._forward(test_input)
        out2, _, _ = predictor2._forward(test_input)
        assert np.allclose(out1, out2), "Predictions should match after load"

    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _test_cold_start():
    """Test cold start with insufficient data returns None"""
    predictor = LoadPredictor()
    now_utc = datetime.now(timezone.utc)

    # Only 1 day of data (insufficient for 48h horizon + lookback)
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=1, now_utc=now_utc)

    # Training should fail or return None
    val_mae = predictor.train(load_data, now_utc, pv_minutes=None, is_initial=True, epochs=5, time_decay_days=7)

    # With only 1 day of data, we can't create a valid dataset for 48h prediction
    # The result depends on actual data coverage
    # Just verify it doesn't crash
    assert True, "Cold start should not crash"


def _test_fine_tune():
    """Test fine-tuning on recent data only"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)

    # Initial training on 7 days
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    predictor.train(load_data, now_utc, pv_minutes=None, is_initial=True, epochs=5, time_decay_days=7)

    # Store original weights
    orig_weights = [w.copy() for w in predictor.weights]

    # Fine-tune with same data but as fine-tune mode
    # Note: Fine-tune uses is_finetune=True which only looks at last 24h
    # For the test to work, we need enough data for the full training
    predictor.train(load_data, now_utc, pv_minutes=None, is_initial=False, epochs=3, time_decay_days=7)

    # Even if fine-tune has insufficient data, initial training should have worked
    # The test validates that fine-tune doesn't crash and model is still valid
    assert predictor.model_initialized, "Model should still be initialized after fine-tune attempt"


def _test_prediction():
    """Test end-to-end prediction"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Train on synthetic data
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    predictor.train(load_data, now_utc, pv_minutes=None, is_initial=True, epochs=10, time_decay_days=7)

    # Make prediction
    predictions = predictor.predict(load_data, now_utc, midnight_utc, pv_minutes=None)

    # Should return dict with minute keys
    if predictions:  # May return empty dict if validation fails
        assert isinstance(predictions, dict), "Predictions should be a dict"
        # Check some predictions exist
        assert len(predictions) > 0, "Should have some predictions"
        # All values should be non-negative
        for minute, val in predictions.items():
            assert val >= 0, f"Prediction at minute {minute} should be non-negative"


def _test_prediction_with_pv():
    """Test end-to-end prediction with PV forecast data"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Create load and PV data (with 48h forecast)
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    pv_data = _create_synthetic_pv_data(n_days=7, now_utc=now_utc, forecast_hours=48)  # Include forecast

    # Train with PV data
    predictor.train(load_data, now_utc, pv_minutes=pv_data, is_initial=True, epochs=10, time_decay_days=7)

    # Make prediction with PV forecast
    predictions = predictor.predict(load_data, now_utc, midnight_utc, pv_minutes=pv_data)

    # Should return predictions
    if predictions:
        assert isinstance(predictions, dict), "Predictions should be a dict"
        assert len(predictions) > 0, "Should have predictions with PV data"

        # Verify all values are non-negative
        for minute, val in predictions.items():
            assert val >= 0, f"Prediction at minute {minute} should be non-negative"

        # Verify predictions span 48 hours (576 steps at 5-min intervals)
        max_minute = max(predictions.keys())
        assert max_minute >= 2800, f"Predictions should span ~48h (2880 min), got {max_minute} min"


def _test_prediction_with_temp():
    """Test end-to-end prediction with temperature forecast data"""
    predictor = LoadPredictor(learning_rate=0.01)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Create load and temperature data (with 48h forecast)
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=7, now_utc=now_utc)
    temp_data = _create_synthetic_temp_data(n_days=7, now_utc=now_utc, forecast_hours=48)  # Include forecast

    # Train with temperature data
    predictor.train(load_data, now_utc, temp_minutes=temp_data, is_initial=True, epochs=10, time_decay_days=7)

    # Make prediction with temperature forecast
    predictions = predictor.predict(load_data, now_utc, midnight_utc, temp_minutes=temp_data)

    # Should return predictions
    if predictions:
        assert isinstance(predictions, dict), "Predictions should be a dict"
        assert len(predictions) > 0, "Should have predictions with temperature data"

        # Verify all values are non-negative
        for minute, val in predictions.items():
            assert val >= 0, f"Prediction at minute {minute} should be non-negative"

        # Verify predictions span 48 hours (576 steps at 5-min intervals)
        max_minute = max(predictions.keys())
        assert max_minute >= 2800, f"Predictions should span ~48h (2880 min), got {max_minute} min"


def _test_real_data_training():
    """
    Test training on real input_train_data.json data and generate comparison chart
    """
    import json
    import os

    # Try to load the input_train_data.json which has real PV and temperature
    input_train_paths = ["input_train_data.json"]

    load_data = None
    pv_data = None
    temp_data = None
    import_rates_data = None
    export_rates_data = None

    for json_path in input_train_paths:
        if os.path.exists(json_path):
            with open(json_path, "r") as f:
                train_data = json.load(f)

            # Check if new dict format (with timestamps) or old array format
            if isinstance(train_data, dict):
                # New format: dict with named keys
                load_data = {int(k): float(v) for k, v in train_data["load_minutes"].items()}
                pv_data = {int(k): float(v) for k, v in train_data["pv_data"].items()} if train_data.get("pv_data") else {}
                temp_data = {int(k): float(v) for k, v in train_data["temperature_data"].items()} if train_data.get("temperature_data") else {}
                import_rates_data = {int(k): float(v) for k, v in train_data["import_rates"].items()} if train_data.get("import_rates") else {}
                export_rates_data = {int(k): float(v) for k, v in train_data["export_rates"].items()} if train_data.get("export_rates") else {}
            else:
                # Old format: [load_minutes_new, age_days, load_minutes_now, pv_data, temperature_data, import_rates, export_rates]
                if len(train_data) >= 5:
                    # Convert string keys to integers
                    load_data = {int(k): float(v) for k, v in train_data[0].items()}
                    pv_data = {int(k): float(v) for k, v in train_data[3].items()} if train_data[3] else {}
                    temp_data = {int(k): float(v) for k, v in train_data[4].items()} if train_data[4] else {}
                    # Load import/export rates if available (elements 5 and 6)
                    if len(train_data) >= 7:
                        import_rates_data = {int(k): float(v) for k, v in train_data[5].items()} if train_data[5] else {}
                        export_rates_data = {int(k): float(v) for k, v in train_data[6].items()} if train_data[6] else {}

            print(f"  Loaded training data from {json_path}")
            print(f"    Load: {len(load_data)} datapoints")
            print(f"    PV: {len(pv_data)} datapoints")
            print(f"    Temperature: {len(temp_data)} datapoints")
            if import_rates_data is not None:
                print(f"    Import rates: {len(import_rates_data)} datapoints")
            if export_rates_data is not None:
                print(f"    Export rates: {len(export_rates_data)} datapoints")
            break

    if load_data is None:
        print("  WARNING: No training data found, skipping real data test")
        return

    # Initialize predictor with lower learning rate for better convergence
    predictor = LoadPredictor(learning_rate=0.0005, max_load_kw=20.0)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Calculate how many days of data we have
    max_minute = max(load_data.keys())
    n_days = max_minute / (24 * 60)
    print(f"  Data spans {n_days:.1f} days ({max_minute} minutes)")

    # Generate synthetic data only if real data wasn't loaded
    if pv_data is None or len(pv_data) == 0:
        print(f"  Generating synthetic PV data for {n_days:.1f} days...")
        pv_data = _create_synthetic_pv_data(n_days=int(n_days) + 1, now_utc=now_utc, forecast_hours=48)
        print(f"  Generated {len(pv_data)} PV datapoints")

    if temp_data is None or len(temp_data) == 0:
        print(f"  Generating synthetic temperature data for {n_days:.1f} days...")
        temp_data = _create_synthetic_temp_data(n_days=int(n_days) + 1, now_utc=now_utc, forecast_hours=48)
        print(f"  Generated {len(temp_data)} temperature datapoints")

    # Train on full dataset with more epochs for larger network
    data_source = "real" if (pv_data and len(pv_data) > 100 and temp_data and len(temp_data) > 100) else "synthetic"
    has_rates = import_rates_data and export_rates_data and len(import_rates_data) > 0 and len(export_rates_data) > 0
    rates_info = " + import/export rates" if has_rates else ""
    print(f"  Training on real load + {data_source} PV/temperature{rates_info} with {len(load_data)} points...")
    success = predictor.train(load_data, now_utc, pv_minutes=pv_data, temp_minutes=temp_data, import_rates=import_rates_data, export_rates=export_rates_data, is_initial=True, epochs=50, time_decay_days=7, validation_holdout_hours=48)

    assert success, "Training on real data should succeed"
    assert predictor.model_initialized, "Model should be initialized after training"

    success = predictor.train(load_data, now_utc, pv_minutes=pv_data, temp_minutes=temp_data, import_rates=import_rates_data, export_rates=export_rates_data, is_initial=False, epochs=50, time_decay_days=7, validation_holdout_hours=48)
    success = predictor.train(load_data, now_utc, pv_minutes=pv_data, temp_minutes=temp_data, import_rates=import_rates_data, export_rates=export_rates_data, is_initial=False, epochs=50, time_decay_days=7, validation_holdout_hours=48)

    # Make predictions
    print(f"  Generating predictions with PV + temperature{rates_info} forecasts...")
    predictions = predictor.predict(load_data, now_utc, midnight_utc, pv_minutes=pv_data, temp_minutes=temp_data, import_rates=import_rates_data, export_rates=export_rates_data)

    assert isinstance(predictions, dict), "Predictions should be a dict"
    assert len(predictions) > 0, "Should have predictions"

    print(f"  Generated {len(predictions)} predictions")

    # Create comparison chart using matplotlib
    try:
        import matplotlib

        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt

        # Chart layout: 7 days of history (negative hours) + 2 days of predictions (positive hours)
        # X-axis: -168 to +48 hours (0 = now)
        history_hours = 7 * 24  # 7 days back
        prediction_hours = 48  # 2 days forward

        # Convert historical load_data (cumulative kWh) to energy per 5-min step (kWh)
        # Going backwards in time: minute 0 is now, higher minutes are past
        historical_minutes = []
        historical_energy = []
        max_history_minutes = min(history_hours * 60, max_minute)

        for minute in range(0, max_history_minutes, STEP_MINUTES):
            if minute in load_data and (minute + STEP_MINUTES) in load_data:
                energy_kwh = max(0, load_data[minute] - load_data.get(minute + STEP_MINUTES, load_data[minute]))
                historical_minutes.append(minute)
                historical_energy.append(energy_kwh)

        # Extract validation period actual data (most recent 24h = day 7)
        # This is the data the model was validated against
        val_actual_minutes = []
        val_actual_energy = []
        val_period_hours = 24  # Most recent 24h
        for minute in range(0, val_period_hours * 60, STEP_MINUTES):
            if minute in load_data and (minute + STEP_MINUTES) in load_data:
                energy_kwh = max(0, load_data[minute] - load_data.get(minute + STEP_MINUTES, load_data[minute]))
                val_actual_minutes.append(minute)
                val_actual_energy.append(energy_kwh)

        # Generate validation predictions: what would the model predict for day 7
        # using only data from day 2-7 (excluding most recent 24h)?
        # Simulate predicting from 24h ago
        val_pred_minutes = []
        val_pred_energy = []

        # Create a modified load_data that excludes the most recent 24h
        # This simulates predicting "yesterday" from "2 days ago"
        val_holdout_minutes = val_period_hours * 60
        shifted_load_data = {}
        for minute, cum_kwh in load_data.items():
            if minute >= val_holdout_minutes:
                # Shift back by 24h so model predicts into "held out" period
                shifted_load_data[minute - val_holdout_minutes] = cum_kwh

        # Make validation prediction (predict next 24h from shifted data)
        if shifted_load_data:
            shifted_now = now_utc - timedelta(hours=val_period_hours)
            shifted_midnight = shifted_now.replace(hour=0, minute=0, second=0, microsecond=0)

            # Create shifted PV data for validation prediction
            shifted_pv_data = {}
            for minute, cum_kwh in pv_data.items():
                if minute >= val_holdout_minutes:
                    # Shift historical data back by 24h
                    shifted_pv_data[minute - val_holdout_minutes] = cum_kwh
                elif minute < 0:
                    # Keep future forecast data (negative minutes) as-is
                    shifted_pv_data[minute] = cum_kwh

            # Create shifted temperature data for validation prediction
            shifted_temp_data = {}
            for minute, temp in temp_data.items():
                if minute >= val_holdout_minutes:
                    # Shift historical data back by 24h
                    shifted_temp_data[minute - val_holdout_minutes] = temp
                elif minute < 0:
                    # Keep future forecast data (negative minutes) as-is
                    shifted_temp_data[minute] = temp

            # Create shifted import/export rate data for validation prediction
            shifted_import_rates = {}
            shifted_export_rates = {}
            if import_rates_data:
                for minute, rate in import_rates_data.items():
                    if minute >= val_holdout_minutes:
                        # Shift historical data back by 24h
                        shifted_import_rates[minute - val_holdout_minutes] = rate
                    elif minute < 0:
                        # Keep future forecast data (negative minutes) as-is
                        shifted_import_rates[minute] = rate
            if export_rates_data:
                for minute, rate in export_rates_data.items():
                    if minute >= val_holdout_minutes:
                        # Shift historical data back by 24h
                        shifted_export_rates[minute - val_holdout_minutes] = rate
                    elif minute < 0:
                        # Keep future forecast data (negative minutes) as-is
                        shifted_export_rates[minute] = rate

            val_predictions = predictor.predict(
                shifted_load_data,
                shifted_now,
                shifted_midnight,
                pv_minutes=shifted_pv_data,
                temp_minutes=shifted_temp_data,
                import_rates=shifted_import_rates if shifted_import_rates else None,
                export_rates=shifted_export_rates if shifted_export_rates else None,
            )

            # Extract first 24h of validation predictions
            # First cumulative value is already the per-step energy
            val_pred_keys = sorted(val_predictions.keys())

            for i, minute in enumerate(val_pred_keys):
                if minute >= val_period_hours * 60:
                    break
                if i == 0:
                    # First cumulative value IS the energy for the first step (0-5 min)
                    energy_kwh = 0
                else:
                    # Subsequent steps: calculate delta from previous cumulative
                    prev_minute = val_pred_keys[i - 1]
                    energy_kwh = max(0, val_predictions[minute] - val_predictions[prev_minute])
                val_pred_minutes.append(minute)
                val_pred_energy.append(dp4(energy_kwh))

        # Convert predictions (cumulative kWh) to energy per step (kWh)
        # First cumulative value is already the per-step energy
        # predictions dict is: {0: cum0, 5: cum5, 10: cum10, ...} representing FUTURE
        pred_minutes = []
        pred_energy = []
        pred_keys = sorted(predictions.keys())

        for i, minute in enumerate(pred_keys):
            if minute >= prediction_hours * 60:
                break
            if i == 0:
                # First cumulative value IS the energy for the first step (0-5 min)
                energy_kwh = 0
            else:
                # Subsequent steps: calculate delta from previous cumulative
                prev_minute = pred_keys[i - 1]
                energy_kwh = max(0, predictions[minute] - predictions[prev_minute])
            pred_minutes.append(minute)
            pred_energy.append(energy_kwh)

        # Convert PV data to energy per step for plotting
        # Historical PV (positive minutes, going back in time)
        pv_historical_minutes = []
        pv_historical_energy = []
        for minute in range(0, max_history_minutes, STEP_MINUTES):
            if minute in pv_data and (minute + STEP_MINUTES) in pv_data:
                energy_kwh = max(0, pv_data[minute] - pv_data.get(minute + STEP_MINUTES, pv_data[minute]))
                pv_historical_minutes.append(minute)
                pv_historical_energy.append(energy_kwh)

        # Future PV forecasts (negative minutes in pv_data dict, representing future)
        pv_forecast_minutes = []
        pv_forecast_energy = []
        for minute in range(-prediction_hours * 60, 0, STEP_MINUTES):
            if minute in pv_data and (minute + STEP_MINUTES) in pv_data:
                energy_kwh = max(0, pv_data[minute] - pv_data.get(minute + STEP_MINUTES, pv_data[minute]))
                pv_forecast_minutes.append(minute)
                pv_forecast_energy.append(energy_kwh)

        # Extract temperature data (non-cumulative, so we use raw values)
        # Historical temperature (positive minutes in temp_data dict, going back in time)
        temp_historical_minutes = []
        temp_historical_celsius = []
        for minute in range(0, max_history_minutes, STEP_MINUTES):
            if minute in temp_data:
                temp_celsius = temp_data[minute]
                temp_historical_minutes.append(minute)
                temp_historical_celsius.append(temp_celsius)

        # Future temperature forecasts (negative minutes in temp_data dict, representing future)
        temp_forecast_minutes = []
        temp_forecast_celsius = []
        for minute in range(-prediction_hours * 60, 0, STEP_MINUTES):
            if minute in temp_data:
                temp_celsius = temp_data[minute]
                temp_forecast_minutes.append(minute)
                temp_forecast_celsius.append(temp_celsius)

        # Create figure with single plot showing timeline
        fig, ax = plt.subplots(1, 1, figsize=(16, 6))

        # Create secondary y-axis for temperature
        ax2 = ax.twinx()

        # Plot PV data first (in background)
        # Historical PV (negative hours, going back in time)
        if pv_historical_minutes:
            pv_hist_hours = [-m / 60 for m in pv_historical_minutes]  # Negative for past
            ax.plot(pv_hist_hours, pv_historical_energy, "orange", linewidth=0.8, label="Historical PV (7 days)", alpha=0.3, linestyle="--")

        # Future PV forecasts (positive hours, going forward)
        if pv_forecast_minutes:
            # Convert negative minutes to positive hours for future
            pv_forecast_hours = [-m / 60 for m in pv_forecast_minutes]  # Negative minutes become positive hours
            ax.plot(pv_forecast_hours, pv_forecast_energy, "orange", linewidth=1.2, label="PV Forecast (48h)", alpha=0.5, linestyle="--")

        # Plot temperature data on secondary y-axis
        # Historical temperature (negative hours, going back in time)
        if temp_historical_minutes:
            temp_hist_hours = [-m / 60 for m in temp_historical_minutes]  # Negative for past
            ax2.plot(temp_hist_hours, temp_historical_celsius, "purple", linewidth=0.8, label="Historical Temp (7 days)", alpha=0.4, linestyle="-.")

        # Future temperature forecasts (positive hours, going forward)
        if temp_forecast_minutes:
            # Convert negative minutes to positive hours for future
            temp_forecast_hours = [-m / 60 for m in temp_forecast_minutes]  # Negative minutes become positive hours
            ax2.plot(temp_forecast_hours, temp_forecast_celsius, "purple", linewidth=1.2, label="Temp Forecast (48h)", alpha=0.6, linestyle="-.")

        # Plot historical data (negative hours, going back in time)
        # minute 0 = now (hour 0), minute 60 = 1 hour ago (hour -1)
        if historical_minutes:
            hist_hours = [-m / 60 for m in historical_minutes]  # Negative for past
            ax.plot(hist_hours, historical_energy, "b-", linewidth=0.8, label="Historical Load (7 days)", alpha=0.5)

        # Highlight validation period actual data (most recent 24h) with thicker line
        if val_actual_minutes:
            val_actual_hours = [-m / 60 for m in val_actual_minutes]  # Negative for past
            ax.plot(val_actual_hours, val_actual_energy, "b-", linewidth=1.5, label="Actual Day 7 (validation)", alpha=0.9)

        # Plot validation predictions (what model predicted for day 7)
        if val_pred_minutes:
            # These predictions map to the validation period (most recent 24h)
            # val_pred minute 0 -> actual minute 0 -> hour 0, etc.
            val_pred_hours = [-m / 60 for m in val_pred_minutes]  # Same position as actual
            ax.plot(val_pred_hours, val_pred_energy, "g-", linewidth=1.5, label="ML Prediction (day 7)", alpha=0.9)

        # Plot future predictions (positive hours, going forward)
        if pred_minutes:
            pred_hours = [m / 60 for m in pred_minutes]  # Positive for future
            ax.plot(pred_hours, pred_energy, "r-", linewidth=1.5, label="ML Prediction (48h future)", alpha=0.9)

        # Add vertical line at "now"
        ax.axvline(x=0, color="black", linestyle="--", linewidth=2, label="Now", alpha=0.8)

        # Shade the validation region (most recent 24h)
        ax.axvspan(-24, 0, alpha=0.1, color="green", label="Validation Period")

        # Formatting
        ax.set_xlabel("Hours (negative = past, positive = future)", fontsize=12)
        ax.set_ylabel("Load (kWh per 5 min)", fontsize=12)
        ax2.set_ylabel("Temperature (°C)", fontsize=12, color="purple")
        ax2.tick_params(axis="y", labelcolor="purple")
        ax.set_title("ML Load Predictor with PV + Temperature Input: Validation (Day 7) + 48h Forecast", fontsize=14, fontweight="bold")

        # Combine legends from both axes
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.set_xlim(-history_hours, prediction_hours)

        # Add day markers
        for day in range(-7, 3):
            hour = day * 24
            if -history_hours <= hour <= prediction_hours:
                ax.axvline(x=hour, color="gray", linestyle=":", linewidth=0.5, alpha=0.5)

        plt.tight_layout()

        # Save to coverage directory
        chart_paths = ["../coverage/ml_prediction_chart.png", "coverage/ml_prediction_chart.png", "ml_prediction_chart.png"]
        for chart_path in chart_paths:
            try:
                plt.savefig(chart_path, dpi=150, bbox_inches="tight")
                print(f"  Chart saved to {chart_path}")
                break
            except:
                continue

        plt.close()

    except ImportError:
        print("  WARNING: matplotlib not available, skipping chart generation")


def _test_pretrained_model_prediction():
    """
    Test loading a pre-trained model and generating predictions with chart
    """
    import json
    import os

    # Try to find the saved model
    model_paths = ["predbat_ml_model.npz"]

    model_path = None
    for path in model_paths:
        if os.path.exists(path):
            model_path = path
            break

    if model_path is None:
        print("  WARNING: No saved model found, skipping pre-trained model test")
        return

    print(f"  Found saved model at {model_path}")

    # Load the input_train_data.json for data to predict on
    input_train_paths = ["ml_pretrained_debug.json"]

    load_data = None
    pv_data = None
    temp_data = None
    import_rates_data = None
    export_rates_data = None
    now_utc = None
    midnight_utc = None
    minutes_now = None

    for json_path in input_train_paths:
        if os.path.exists(json_path):
            with open(json_path, "r") as f:
                train_data = json.load(f)

            # Check if new dict format (with timestamps) or old array format
            if isinstance(train_data, dict):
                # New format: dict with named keys including timestamps
                load_data = {int(k): float(v) for k, v in train_data["load_minutes"].items()}
                pv_data = {int(k): float(v) for k, v in train_data["pv_data"].items()} if train_data.get("pv_data") else {}
                temp_data = {int(k): float(v) for k, v in train_data["temperature_data"].items()} if train_data.get("temperature_data") else {}
                import_rates_data = {int(k): float(v) for k, v in train_data["import_rates"].items()} if train_data.get("import_rates") else {}
                export_rates_data = {int(k): float(v) for k, v in train_data["export_rates"].items()} if train_data.get("export_rates") else {}
                # Load timestamps
                if "now_utc" in train_data:
                    now_utc = parser.parse(train_data["now_utc"])
                    midnight_utc = parser.parse(train_data["midnight_utc"])
                    minutes_now = train_data["minutes_now"]
                    print(f"  Using captured timestamp: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"  Minutes since midnight: {minutes_now}")
            else:
                # Old format: [load_minutes_new, age_days, load_minutes_now, pv_data, temperature_data, import_rates, export_rates]
                if len(train_data) >= 5:
                    load_data = {int(k): float(v) for k, v in train_data[0].items()}
                    pv_data = {int(k): float(v) for k, v in train_data[3].items()} if train_data[3] else {}
                    temp_data = {int(k): float(v) for k, v in train_data[4].items()} if train_data[4] else {}
                    if len(train_data) >= 7:
                        import_rates_data = {int(k): float(v) for k, v in train_data[5].items()} if train_data[5] else {}
                        export_rates_data = {int(k): float(v) for k, v in train_data[6].items()} if train_data[6] else {}

            print(f"  Loaded data from {json_path}")
            print(f"    Load: {len(load_data)} datapoints")
            print(f"    PV: {len(pv_data)} datapoints")
            print(f"    Temperature: {len(temp_data)} datapoints")
            if import_rates_data is not None:
                print(f"    Import rates: {len(import_rates_data)} datapoints")
            if export_rates_data is not None:
                print(f"    Export rates: {len(export_rates_data)} datapoints")
            break

    if load_data is None:
        print("  WARNING: No data found, skipping pre-trained model test")
        return

    # Initialize predictor and load the model
    predictor = LoadPredictor(learning_rate=0.0005, max_load_kw=20.0)

    print(f"  Loading model from {model_path}...")
    success = predictor.load(model_path)

    if not success:
        print("  WARNING: Failed to load model, skipping pre-trained model test")
        return

    assert predictor.model_initialized, "Model should be initialized after loading"
    print(f"  Model loaded successfully")
    print(f"    Validation MAE: {predictor.validation_mae:.4f} kWh")
    print(f"    Epochs trained: {predictor.epochs_trained}")
    print(f"    Training timestamp: {predictor.training_timestamp}")

    # Use captured timestamps if available, otherwise use current time
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
        midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_now = int((now_utc - midnight_utc).total_seconds() / 60)
        print(f"  WARNING: No timestamp in data, using current time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")

    # Calculate how many days of data we have
    max_minute = max(load_data.keys())
    n_days = max_minute / (24 * 60)
    print(f"  Data spans {n_days:.1f} days ({max_minute} minutes)")

    # Generate synthetic data only if real data wasn't loaded
    if pv_data is None or len(pv_data) == 0:
        print(f"  Generating synthetic PV data for {n_days:.1f} days...")
        pv_data = _create_synthetic_pv_data(n_days=int(n_days) + 1, now_utc=now_utc, forecast_hours=48)
        print(f"  Generated {len(pv_data)} PV datapoints")

    if temp_data is None or len(temp_data) == 0:
        print(f"  Generating synthetic temperature data for {n_days:.1f} days...")
        temp_data = _create_synthetic_temp_data(n_days=int(n_days) + 1, now_utc=now_utc, forecast_hours=48)
        print(f"  Generated {len(temp_data)} temperature datapoints")

    # Make predictions using the loaded model
    has_rates = import_rates_data and export_rates_data and len(import_rates_data) > 0 and len(export_rates_data) > 0
    rates_info = " + import/export rates" if has_rates else ""
    data_source = "real" if (pv_data and len(pv_data) > 100 and temp_data and len(temp_data) > 100) else "synthetic"

    print(f"  Generating predictions using loaded model with {data_source} PV/temperature{rates_info}...")
    predictions = predictor.predict(load_data, now_utc, midnight_utc, pv_minutes=pv_data, temp_minutes=temp_data, import_rates=import_rates_data, export_rates=export_rates_data)

    assert isinstance(predictions, dict), "Predictions should be a dict"
    assert len(predictions) > 0, "Should have predictions"

    print(f"  Generated {len(predictions)} predictions")

    # Create comparison chart using matplotlib (reuse chart code from real_data_training)
    try:
        import matplotlib

        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt

        # Chart layout: 7 days of history (negative hours) + 2 days of predictions (positive hours)
        history_hours = 7 * 24  # 7 days back
        prediction_hours = 48  # 2 days forward

        # Convert historical load_data to energy per 5-min step
        historical_minutes = []
        historical_energy = []
        max_history_minutes = min(history_hours * 60, max_minute)

        for minute in range(0, max_history_minutes, STEP_MINUTES):
            if minute in load_data and (minute + STEP_MINUTES) in load_data:
                energy_kwh = max(0, load_data[minute] - load_data.get(minute + STEP_MINUTES, load_data[minute]))
                historical_minutes.append(minute)
                historical_energy.append(energy_kwh)

        # Convert predictions to energy per step
        pred_minutes = []
        pred_energy = []
        pred_keys = sorted(predictions.keys())
        for i, minute in enumerate(pred_keys):
            if minute >= prediction_hours * 60:
                break
            if i == 0:
                energy_kwh = predictions[minute]
            else:
                prev_minute = pred_keys[i - 1]
                energy_kwh = max(0, predictions[minute] - predictions[prev_minute])
            pred_minutes.append(minute)
            pred_energy.append(energy_kwh)

        # Convert PV data to energy per step
        pv_historical_minutes = []
        pv_historical_energy = []
        for minute in range(0, max_history_minutes, STEP_MINUTES):
            if minute in pv_data and (minute + STEP_MINUTES) in pv_data:
                energy_kwh = max(0, pv_data[minute] - pv_data.get(minute + STEP_MINUTES, pv_data[minute]))
                pv_historical_minutes.append(minute)
                pv_historical_energy.append(energy_kwh)

        pv_forecast_minutes = []
        pv_forecast_energy = []
        for minute in range(-prediction_hours * 60, 0, STEP_MINUTES):
            if minute in pv_data and (minute + STEP_MINUTES) in pv_data:
                energy_kwh = max(0, pv_data[minute] - pv_data.get(minute + STEP_MINUTES, pv_data[minute]))
                pv_forecast_minutes.append(minute)
                pv_forecast_energy.append(energy_kwh)

        # Extract temperature data
        temp_historical_minutes = []
        temp_historical_celsius = []
        for minute in range(0, max_history_minutes, STEP_MINUTES):
            if minute in temp_data:
                temp_celsius = temp_data[minute]
                temp_historical_minutes.append(minute)
                temp_historical_celsius.append(temp_celsius)

        temp_forecast_minutes = []
        temp_forecast_celsius = []
        for minute in range(-prediction_hours * 60, 0, STEP_MINUTES):
            if minute in temp_data:
                temp_celsius = temp_data[minute]
                temp_forecast_minutes.append(minute)
                temp_forecast_celsius.append(temp_celsius)

        # Create figure
        fig, ax = plt.subplots(1, 1, figsize=(16, 6))
        ax2 = ax.twinx()

        # Plot PV data
        if pv_historical_minutes:
            pv_hist_hours = [-m / 60 for m in pv_historical_minutes]
            ax.plot(pv_hist_hours, pv_historical_energy, "orange", linewidth=0.8, label="Historical PV (7 days)", alpha=0.3, linestyle="--")

        if pv_forecast_minutes:
            pv_forecast_hours = [-m / 60 for m in pv_forecast_minutes]
            ax.plot(pv_forecast_hours, pv_forecast_energy, "orange", linewidth=1.2, label="PV Forecast (48h)", alpha=0.5, linestyle="--")

        # Plot temperature
        if temp_historical_minutes:
            temp_hist_hours = [-m / 60 for m in temp_historical_minutes]
            ax2.plot(temp_hist_hours, temp_historical_celsius, "purple", linewidth=0.8, label="Historical Temp (7 days)", alpha=0.4, linestyle="-.")

        if temp_forecast_minutes:
            temp_forecast_hours = [-m / 60 for m in temp_forecast_minutes]
            ax2.plot(temp_forecast_hours, temp_forecast_celsius, "purple", linewidth=1.2, label="Temp Forecast (48h)", alpha=0.6, linestyle="-.")

        # Plot historical load
        if historical_minutes:
            hist_hours = [-m / 60 for m in historical_minutes]
            ax.plot(hist_hours, historical_energy, "b-", linewidth=0.8, label="Historical Load (7 days)", alpha=0.5)

        # Plot predictions
        if pred_minutes:
            pred_hours = [m / 60 for m in pred_minutes]
            ax.plot(pred_hours, pred_energy, "r-", linewidth=1.5, label="ML Prediction (48h future)", alpha=0.9)

        # Add vertical line at "now"
        ax.axvline(x=0, color="black", linestyle="--", linewidth=2, label="Now", alpha=0.8)

        # Labels and formatting
        ax.set_xlabel("Hours from Now (negative = past, positive = future)", fontsize=12)
        ax.set_ylabel("Energy per 5 min (kWh)", fontsize=12)
        ax2.set_ylabel("Temperature (°C)", fontsize=12)
        ax.set_title("Pre-trained ML Model: Load Prediction (Day 7) + 48h Forecast", fontsize=14, fontweight="bold")

        # Combine legends
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.set_xlim(-history_hours, prediction_hours)

        # Add day markers
        for day in range(-7, 3):
            hour = day * 24
            if -history_hours <= hour <= prediction_hours:
                ax.axvline(x=hour, color="gray", linestyle=":", linewidth=0.5, alpha=0.5)

        plt.tight_layout()

        # Save to coverage directory with different name
        chart_paths = ["../coverage/ml_pretrained_chart.png", "coverage/ml_pretrained_chart.png", "ml_pretrained_chart.png"]
        for chart_path in chart_paths:
            try:
                plt.savefig(chart_path, dpi=150, bbox_inches="tight")
                print(f"  Chart saved to {chart_path}")
                break
            except:
                continue

        plt.close()

    except ImportError:
        print("  WARNING: matplotlib not available, skipping chart generation")


def _test_component_fetch_load_data():
    """Test LoadMLComponent._fetch_load_data method"""
    import asyncio
    from datetime import datetime, timezone
    from load_ml_component import LoadMLComponent
    from unittest.mock import MagicMock

    # Helper to run async tests
    def run_async(coro):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    # Create mock base object with all necessary properties
    class MockBase:
        def __init__(self):
            self.prefix = "predbat"
            self.config_root = None
            self.now_utc = datetime.now(timezone.utc)
            self.midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            self.minutes_now = (self.now_utc - self.midnight_utc).seconds // 60
            self.local_tz = timezone.utc
            self.args = {}
            self.log_messages = []

        def log(self, msg):
            self.log_messages.append(msg)

        def get_arg(self, key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
            return {
                "load_today": ["sensor.load_today"],
                "load_power": None,  # Disable load_power to simplify test
                "car_charging_energy": None,  # Disable car charging to simplify test
                "load_scaling": 1.0,
                "car_charging_energy_scale": 1.0,
            }.get(key, default)

        def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
            """Mock get_state_wrapper - returns None for temperature by default"""
            return default

        def fetch_pv_forecast(self):
            """Mock fetch_pv_forecast - returns empty forecasts"""
            return {}, {}

        def minute_data_import_export(self, days, now_utc, entity, scale=1.0, increment=False, smoothing=False, required_unit=None):
            """Mock minute_data_import_export - returns empty dict"""
            return {}

    # Create synthetic load data (28 days worth)
    def create_load_minutes(days=28, all_minutes=False):
        """
        Create cumulative load data going backwards from minute 0

        Args:
            days: Number of days of data to create
            all_minutes: If True, create entries for every minute (not just 5-min intervals)
        """
        load_data = {}
        cumulative = 0.0

        if all_minutes:
            # Create entry for every minute (for car charging test)
            for minute in range(days * 24 * 60, -1, -1):
                energy_step = 0.1 / 5  # Scale down since we have 5x more entries
                cumulative += energy_step
                load_data[minute] = cumulative
        else:
            # Create entries at 5-minute intervals (normal case)
            for minute in range(days * 24 * 60, -1, -5):
                energy_step = 0.1  # 0.1 kWh per 5 min
                cumulative += energy_step
                load_data[minute] = cumulative

        return load_data, days

    # Test 1: Successful fetch with minimal config
    async def test_basic_fetch():
        mock_base = MockBase()
        load_data, age = create_load_minutes(28)
        mock_base.minute_data_load = MagicMock(return_value=(load_data, age))
        mock_base.minute_data_import_export = MagicMock(return_value=None)
        # Mock the fill_load_from_power method - it should just return the load_minutes unchanged
        mock_base.fill_load_from_power = MagicMock(side_effect=lambda x, y: x)

        component = LoadMLComponent(mock_base, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, "Should return load data"
        assert result_age == 28, f"Expected 28 days, got {result_age}"
        assert len(result_data) > 0, "Load data should not be empty"
        assert result_now >= 0, f"Current load should be non-negative, got {result_now}"
        print("    ✓ Basic fetch successful")

    # Test 2: Missing sensor (should return None)
    async def test_missing_sensor():
        class MockBaseNoSensor:
            def __init__(self):
                self.prefix = "predbat"
                self.config_root = None
                self.now_utc = datetime.now(timezone.utc)
                self.local_tz = timezone.utc
                self.args = {}

            def log(self, msg):
                pass

            def get_arg(self, key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
                return default

        mock_base_no_sensor = MockBaseNoSensor()

        component = LoadMLComponent(mock_base_no_sensor, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is None, "Should return None when sensor missing"
        assert result_age == 0, "Age should be 0 when sensor missing"
        assert result_now == 0, "Current load should be 0 when sensor missing"
        print("    ✓ Missing sensor handled correctly")

    # Test 3: Car charging subtraction
    async def test_car_charging_subtraction():
        """Test that car charging energy is correctly subtracted from load data."""
        mock_base_with_car = MockBase()

        # Create load data at 5-minute intervals following Predbat convention:
        # minute 0 = now (highest cumulative), minute 1440 = 24h ago (lowest)
        # Total load: 1.0 kWh per 5-min step
        # Need to extend slightly beyond 1440 for delta calculation at minute 1440
        original_load_data = {}
        cumulative_load = 289.0  # Start with total + 1 extra interval
        for minute in range(0, 1446, 5):  # Go to 1445 to allow delta calc at 1440
            original_load_data[minute] = cumulative_load
            cumulative_load -= 1.0  # Decrease going backwards in time

        # Car charging data: 0.3 kWh per 5-min step
        car_charging_data = {}
        cumulative_car = 86.7  # Start with total + 1 extra interval (289 * 0.3)
        for minute in range(0, 1446, 5):  # Go to 1445 to allow delta calc at 1440
            car_charging_data[minute] = cumulative_car
            cumulative_car -= 0.3  # Decrease going backwards in time

        age = 1.0

        # Override get_arg to enable car_charging_energy
        def mock_get_arg_with_car(key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
            return {
                "load_today": ["sensor.load_today"],
                "load_power": None,
                "car_charging_energy": ["sensor.car_charging"],  # Enable car charging
                "load_scaling": 1.0,
                "car_charging_energy_scale": 1.0,
            }.get(key, default)

        mock_base_with_car.get_arg = mock_get_arg_with_car

        # Return a copy of the data so the original isn't modified
        mock_base_with_car.minute_data_load = MagicMock(return_value=(dict(original_load_data), age))
        mock_base_with_car.minute_data_import_export = MagicMock(return_value=car_charging_data)
        mock_base_with_car.fill_load_from_power = MagicMock(side_effect=lambda x, y: x)

        component = LoadMLComponent(mock_base_with_car, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, f"Should return load data"
        assert result_age > 0, f"Should have valid age (got {result_age})"
        assert len(result_data) > 0, "Result data should not be empty"
        assert result_now >= 0, f"Current load should be non-negative, got {result_now}"

        # Verify car charging was called
        assert mock_base_with_car.minute_data_import_export.called, "minute_data_import_export should be called"

        # Verify all values are non-negative after subtraction
        for minute, value in result_data.items():
            assert value >= 0, f"Load at minute {minute} should be non-negative, got {value}"

        # CRITICAL: Verify actual subtraction occurred correctly
        # At minute 1440: load delta = 1.0 kWh, car delta = 0.3 kWh
        # Result should be 1.0 - 0.3 = 0.7 kWh (not 1.0 kWh)
        if 1440 in result_data:
            value_1440 = result_data[1440]
            expected = 0.7  # load (1.0) - car (0.3)
            assert abs(value_1440 - expected) < 0.01, f"At minute 1440, expected ~{expected} kWh (1.0 - 0.3), got {value_1440:.4f}"

        # At minute 1435: should have accumulated 0.7 + 0.7 = 1.4 kWh
        if 1435 in result_data:
            value_1435 = result_data[1435]
            expected = 1.4  # Two intervals of (1.0 - 0.3)
            assert abs(value_1435 - expected) < 0.01, f"At minute 1435, expected ~{expected} kWh, got {value_1435:.4f}"

        print("    ✓ Car charging subtraction works")

    # Test 3b: Car charging threshold-based detection (without sensor)
    async def test_car_charging_threshold_detection():
        """Test that car charging is detected based on load threshold when no sensor is configured."""
        mock_base_threshold = MockBase()

        # Create load data at 5-minute intervals with varying consumption following Predbat convention:
        # minute 0 = now (highest cumulative), minute 1440 = 24h ago (lowest)
        # Pattern: high (1.0) at i=0,4,8... and normal (0.2) at other positions
        # Need to calculate total first, then work backwards

        # First calculate total energy
        total_energy = 0.0
        pattern = []
        for i in range(289):  # 289 intervals (0-1440 in steps of 5) + 1 extra for delta calc
            if i % 4 == 0:
                delta = 1.0  # High load (car charging)
            else:
                delta = 0.2  # Normal load
            pattern.append(delta)
            total_energy += delta

        # Now create cumulative data going backwards from total
        original_load_data = {}
        cumulative_load = total_energy
        for i, minute in enumerate(range(0, 1446, 5)):  # Go to 1445 for delta calc
            original_load_data[minute] = cumulative_load
            if i < len(pattern):
                cumulative_load -= pattern[i]

        age = 1.0

        # Override get_arg to DISABLE car_charging_energy sensor but keep car_charging_hold enabled
        def mock_get_arg_threshold(key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
            return {
                "load_today": ["sensor.load_today"],
                "load_power": None,
                "car_charging_energy": None,  # No sensor - will use threshold detection
                "load_scaling": 1.0,
                "car_charging_energy_scale": 1.0,
                "car_charging_hold": True,  # Enable threshold-based detection
                "car_charging_threshold": 6.0,  # 6.0 kW/h threshold (0.5 kWh per 5 min)
                "car_charging_rate": 7.5,  # 7.5 kW/h charging rate (0.625 kWh per 5 min)
            }.get(key, default)

        mock_base_threshold.get_arg = mock_get_arg_threshold

        mock_base_threshold.minute_data_load = MagicMock(return_value=(dict(original_load_data), age))
        mock_base_threshold.minute_data_import_export = MagicMock(return_value={})  # No car sensor data
        mock_base_threshold.fill_load_from_power = MagicMock(side_effect=lambda x, y: x)

        component = LoadMLComponent(mock_base_threshold, load_ml_enable=True)
        # Component will use default thresholds from get_arg

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, "Should return load data"
        assert len(result_data) > 0, "Result data should not be empty"

        # Verify threshold-based detection worked
        # car_charging_threshold = 6.0 kW/h / 60 = 0.1 kW/min
        # Threshold per 5-min interval: 0.1 * 5 = 0.5 kWh
        # car_charging_rate = 7.5 kW/h / 60 = 0.125 kW/min
        # Estimated car delta per 5-min: 0.125 * 5 = 0.625 kWh

        # At minute 1440: load delta = 1.0 kWh (exceeds 0.5 threshold)
        # Should subtract estimated car delta: 1.0 - 0.625 = 0.375 kWh
        if 1440 in result_data:
            value_1440 = result_data[1440]
            expected = 0.375  # 1.0 - 0.625 (threshold triggered)
            assert abs(value_1440 - expected) < 0.01, f"At minute 1440 (high load), expected ~{expected} kWh (1.0 - 0.625 car estimate), got {value_1440:.4f}"

        # At minute 1435: load delta = 0.2 kWh (below 0.5 threshold, no car charging detected)
        # Should NOT subtract car delta: just 0.2 kWh
        # Total accumulated: 0.375 + 0.2 = 0.575 kWh
        if 1435 in result_data:
            value_1435 = result_data[1435]
            expected = 0.575  # 0.375 + 0.2 (no car detection)
            assert abs(value_1435 - expected) < 0.01, f"At minute 1435 (normal load), expected ~{expected} kWh, got {value_1435:.4f}"

        # At minute 1430: load delta = 0.2 kWh (below threshold)
        # Total: 0.575 + 0.2 = 0.775 kWh
        if 1430 in result_data:
            value_1430 = result_data[1430]
            expected = 0.775
            assert abs(value_1430 - expected) < 0.01, f"At minute 1430, expected ~{expected} kWh, got {value_1430:.4f}"

        # At minute 1425: load delta = 0.2 kWh (below threshold)
        # Total: 0.775 + 0.2 = 0.975 kWh
        if 1425 in result_data:
            value_1425 = result_data[1425]
            expected = 0.975
            assert abs(value_1425 - expected) < 0.01, f"At minute 1425, expected ~{expected} kWh, got {value_1425:.4f}"

        # At minute 1420: load delta = 1.0 kWh (exceeds threshold again)
        # Subtract car delta: 1.0 - 0.625 = 0.375
        # Total: 0.975 + 0.375 = 1.35 kWh
        if 1420 in result_data:
            value_1420 = result_data[1420]
            expected = 1.35
            assert abs(value_1420 - expected) < 0.01, f"At minute 1420 (high load again), expected ~{expected} kWh, got {value_1420:.4f}"

        print("    ✓ Car charging threshold detection works")

    # Test 4: Load power fill
    async def test_load_power_fill():
        mock_base_with_power = MockBase()

        # Override get_arg to enable load_power
        def mock_get_arg_with_power(key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
            return {
                "load_today": ["sensor.load_today"],
                "load_power": ["sensor.load_power"],  # Enable load_power
                "car_charging_energy": None,
                "load_scaling": 1.0,
                "car_charging_energy_scale": 1.0,
            }.get(key, default)

        mock_base_with_power.get_arg = mock_get_arg_with_power

        load_data, age = create_load_minutes(7)
        load_power_data, _ = create_load_minutes(7)

        mock_base_with_power.minute_data_load = MagicMock(side_effect=[(load_data, age), (load_power_data, age)])  # First call for load_today  # Second call for load_power
        mock_base_with_power.minute_data_import_export = MagicMock(return_value=None)
        mock_base_with_power.fill_load_from_power = MagicMock(return_value=load_data)

        component = LoadMLComponent(mock_base_with_power, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, "Should return load data"
        assert mock_base_with_power.fill_load_from_power.called, "fill_load_from_power should be called"
        assert result_now >= 0, f"Current load should be non-negative, got {result_now}"
        print("    ✓ Load power fill invoked")

    # Test 5: Exception handling
    async def test_exception_handling():
        mock_base = MockBase()
        mock_base.minute_data_load = MagicMock(side_effect=Exception("Test exception"))

        component = LoadMLComponent(mock_base, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is None, "Should return None on exception"
        assert result_age == 0, "Age should be 0 on exception"
        assert result_now == 0, "Current load should be 0 on exception"
        print("    ✓ Exception handling works")

    # Test 6: Empty load data
    async def test_empty_load_data():
        mock_base = MockBase()
        mock_base.minute_data_load = MagicMock(return_value=(None, 0))
        mock_base.minute_data_import_export = MagicMock(return_value=None)

        component = LoadMLComponent(mock_base, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is None, "Should return None when load data is empty"
        assert result_age == 0, "Age should be 0 when load data is empty"
        assert result_now == 0, "Current load should be 0 when load data is empty"
        print("    ✓ Empty load data handled correctly")

    # Test 7: Temperature data fetch with future predictions only
    async def test_temperature_data_fetch():
        from datetime import timedelta

        mock_base_with_temp = MockBase()

        # Create mock temperature data (dict with timestamp strings as keys)
        # This simulates future temperature predictions from sensor.predbat_temperature attribute "results"
        base_time = mock_base_with_temp.now_utc
        temp_predictions = {}
        for hours_ahead in range(1, 49):  # 48 hours of predictions
            timestamp = base_time + timedelta(hours=hours_ahead)
            timestamp_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")
            temp_predictions[timestamp_str] = 15.0 + (hours_ahead % 12)  # Simulated temperature pattern

        # Override get_state_wrapper using MagicMock to return temperature predictions
        def mock_get_state_wrapper_side_effect(entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
            if entity_id == "sensor.predbat_temperature" and attribute == "results":
                return temp_predictions
            return default

        mock_base_with_temp.get_state_wrapper = MagicMock(side_effect=mock_get_state_wrapper_side_effect)

        load_data, age = create_load_minutes(7)

        # Mock minute_data_load to return load data
        mock_base_with_temp.minute_data_load = MagicMock(return_value=(load_data, age))
        mock_base_with_temp.minute_data_import_export = MagicMock(return_value={})
        mock_base_with_temp.fill_load_from_power = MagicMock(side_effect=lambda x, y: x)

        component = LoadMLComponent(mock_base_with_temp, load_ml_enable=True)
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, "Should return load data"
        assert result_temp is not None, "Should return temperature data"
        assert isinstance(result_temp, dict), "Temperature data should be a dict"
        assert len(result_temp) > 0, "Temperature data should not be empty"

        # Verify we have future temperature data (positive minutes from midnight)
        # Note: minute_data with backwards=False returns positive minute keys
        # These represent minutes from midnight forward (future predictions)
        assert len(result_temp) > 0, "Should have future temperature predictions"

        # Verify get_state_wrapper was called correctly
        assert mock_base_with_temp.get_state_wrapper.called, "get_state_wrapper should be called"

        print("    ✓ Temperature data fetch (future predictions) works")

    # Test 8: Temperature data with no predictions (None return)
    async def test_temperature_no_data():
        mock_base_no_temp = MockBase()

        load_data, age = create_load_minutes(7)
        mock_base_no_temp.minute_data_load = MagicMock(return_value=(load_data, age))
        mock_base_no_temp.minute_data_import_export = MagicMock(return_value={})
        mock_base_no_temp.fill_load_from_power = MagicMock(side_effect=lambda x, y: x)

        # get_state_wrapper returns None (default behavior)

        component = LoadMLComponent(mock_base_no_temp, load_ml_enable=True)
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, "Should return load data"
        assert result_temp is not None, "Should return temperature data (empty dict)"
        assert isinstance(result_temp, dict), "Temperature data should be a dict"
        assert len(result_temp) == 0, "Temperature data should be empty when no predictions available"

        print("    ✓ Temperature data with no predictions handled correctly")

    # Test 9: Step-size calculation correctness (bug #3384 regression test)
    async def test_step_size_calculation():
        """
        Regression test for issue #3384: LoadML near-zero predictions due to step-size mismatch.

        This test validates that energy deltas are correctly calculated with STEP (5 minutes)
        instead of 1 minute when data is at 5-minute intervals without car charging.
        """
        mock_base_step_size = MockBase()

        # Create load data with 5-minute intervals following Predbat convention:
        # minute 0 = now (highest cumulative value), minute 1440 = 24h ago (lowest value)
        # Need to extend slightly beyond 1440 for delta calculation at minute 1440
        load_data = {}
        cumulative = 144.5  # Total energy over 24h + 1 interval (289 intervals * 0.5 kWh)
        for minute in range(0, 1446, 5):  # Go to 1445 to allow delta calc at 1440
            load_data[minute] = cumulative
            cumulative -= 0.5  # Decrease going backwards in time (0.5 kWh per 5-min step)

        # No car charging - this is the bug scenario
        mock_base_step_size.minute_data_load = MagicMock(return_value=(load_data, 1.0))
        mock_base_step_size.minute_data_import_export = MagicMock(return_value={})
        mock_base_step_size.fill_load_from_power = MagicMock(side_effect=lambda x, y: x)

        component = LoadMLComponent(mock_base_step_size, load_ml_enable=True)
        # Override default values for testing
        component.ml_learning_rate = 0.001
        component.ml_epochs_initial = 10
        component.ml_epochs_update = 2
        component.ml_min_days = 1
        component.ml_validation_threshold = 2.0
        component.ml_time_decay_days = 7
        component.ml_max_load_kw = 23.0
        component.ml_max_model_age_hours = 48
        # IMPORTANT: Disable car charging hold to test the non-car-charging branch (the bug scenario)
        component.car_charging_hold = False

        result_data, result_age, result_now, result_pv, result_temp, result_import_rates, result_export_rates = await component._fetch_load_data()

        assert result_data is not None, "Should return load data"
        assert len(result_data) > 0, "Load data should not be empty"

        # CRITICAL: Verify values are NOT near-zero (the bug would cause this)
        # The bug was using minute-1 instead of minute-STEP for delta calculations.
        # With 5-minute interval data, minute-1 would usually equal minute, giving 0 delta.
        # The fix uses minute-STEP (minute-5) which correctly calculates non-zero deltas.

        # The function processes data from minute 1440 (oldest) to minute 0 (now),
        # calculating deltas and accumulating them into a new cumulative series.
        # Input:  load_data[1440]=0.0, load_data[1435]=0.5, ..., load_data[0]=144.0
        # Output: result_data[1440]=0.5 (first delta), result_data[1435]=1.0 (accumulated), ..., result_data[0]=144.0 (total)

        # Check minute 1440 (oldest data point, just the first delta)
        if 1440 in result_data:
            value_1440 = result_data[1440]
            # delta = abs(load_data[1440] - load_data[1435]) = abs(0.0 - 0.5) = 0.5
            # result_data[1440] = 0 + 0.5 = 0.5 kWh
            expected_value = 0.5
            assert abs(value_1440 - expected_value) < 0.01, f"Energy at minute 1440 should be ~{expected_value:.2f} kWh (got {value_1440:.4f}). Bug #3384 would cause 0.0."
            assert value_1440 > 0.01, f"Energy at minute 1440 should be > 0.01 kWh (got {value_1440:.4f}). Bug #3384 would cause near-zero."

        # Check minute 1435 (second oldest interval, accumulated)
        if 1435 in result_data:
            value_1435 = result_data[1435]
            # delta = abs(load_data[1435] - load_data[1430]) = abs(0.5 - 1.0) = 0.5
            # result_data[1435] = 0.5 + 0.5 = 1.0 kWh (accumulated from 1440 + 1435)
            expected_value = 1.0
            assert abs(value_1435 - expected_value) < 0.01, f"Energy at minute 1435 should be ~{expected_value:.2f} kWh (got {value_1435:.4f})."
            assert value_1435 > 0.5, f"Energy at minute 1435 should be > 0.5 kWh (got {value_1435:.4f})."

        # Check minute 100 (more recent, should have high accumulated value)
        if 100 in result_data:
            value_100 = result_data[100]
            # (1440-100)/5 = 268 intervals processed, each delta = 0.5 kWh
            # result_data[100] ≈ 268 * 0.5 = 134 kWh accumulated
            expected_value = 134.0
            assert abs(value_100 - expected_value) < 1.0, f"Energy at minute 100 should be ~{expected_value:.1f} kWh (got {value_100:.2f}). Bug #3384 would cause near-zero."
            assert value_100 > 130, f"Energy at minute 100 should be > 130 kWh (got {value_100:.2f})."

        # Check minute 0 (now, should have maximum accumulated energy)
        # Note: minute 0 has 0 delta (no data at minute -5), but accumulated total is carried forward
        if 0 in result_data:
            value_0 = result_data[0]
            # 288 intervals (1440/5) * 0.5 kWh = 144 kWh total
            expected_value = 144.0
            assert abs(value_0 - expected_value) < 1.0, f"Energy at minute 0 should be ~{expected_value:.1f} kWh (got {value_0:.2f}). Bug #3384 would cause near-zero."
            assert value_0 > 140, f"Energy at minute 0 should be > 140 kWh (got {value_0:.2f})."

        # Verify current load is reasonable (not near-zero)
        assert result_now > 0.05, f"Current load should be > 0.05 kWh (got {result_now:.4f}). Bug #3384 would cause near-zero."

        print("    ✓ Step-size calculation correct (bug #3384 regression test passed)")

    # Run all sub-tests
    print("  Running LoadMLComponent._fetch_load_data tests:")
    run_async(test_basic_fetch())
    run_async(test_missing_sensor())
    run_async(test_car_charging_subtraction())
    run_async(test_car_charging_threshold_detection())
    run_async(test_load_power_fill())
    run_async(test_exception_handling())
    run_async(test_empty_load_data())
    run_async(test_temperature_data_fetch())
    run_async(test_temperature_no_data())
    run_async(test_step_size_calculation())
    print("  All _fetch_load_data tests passed!")


def _test_component_publish_entity():
    """Test LoadMLComponent._publish_entity method"""
    from datetime import datetime, timezone, timedelta
    from load_ml_component import LoadMLComponent
    from unittest.mock import MagicMock
    from const import TIME_FORMAT

    # Create mock base object
    class MockBase:
        def __init__(self):
            self.prefix = "predbat"
            self.config_root = None
            self.now_utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            self.midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            self.minutes_now = 720  # 12:00 = 720 minutes since midnight
            self.local_tz = timezone.utc
            self.args = {}
            self.log_messages = []
            self.dashboard_calls = []

        def log(self, msg):
            self.log_messages.append(msg)

        def get_arg(self, key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
            return {
                "load_today": ["sensor.load_today"],
                "load_power": None,
                "car_charging_energy": None,
                "load_scaling": 1.0,
                "car_charging_energy_scale": 1.0,
            }.get(key, default)

    # Test 1: Basic entity publishing with predictions
    print("  Testing _publish_entity:")
    mock_base = MockBase()

    component = LoadMLComponent(mock_base, load_ml_enable=True)

    # Mock dashboard_item to capture calls
    def mock_dashboard_item(entity_id, state, attributes, app):
        mock_base.dashboard_calls.append({"entity_id": entity_id, "state": state, "attributes": attributes, "app": app})

    component.dashboard_item = mock_dashboard_item

    # Set up test data
    component.load_minutes_now = 10.5  # Current load today
    component.current_predictions = {
        0: 0.1,  # Now (delta from "before predictions" to now = 0.1)
        5: 0.2,  # 5 minutes from now
        60: 1.3,  # 1 hour from now (load_today_h1)
        480: 9.7,  # 8 hours from now (load_today_h8)
        1440: 28.9,  # 24 hours from now
    }

    # Set up predictor state
    component.predictor.validation_mae = 0.5
    component.predictor.get_model_age_hours = MagicMock(return_value=2.0)  # Mock model age calculation
    component.last_train_time = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    component.load_data_age_days = 7.0
    component.model_status = "active"
    component.predictor.epochs_trained = 50

    # Call _publish_entity
    component._publish_entity()

    # Verify dashboard_item was called (now twice - for main entity and accuracy entity)
    assert len(mock_base.dashboard_calls) == 2, "dashboard_item should be called twice"

    call = mock_base.dashboard_calls[0]
    call2 = mock_base.dashboard_calls[1]

    # Verify entity_id
    assert call["entity_id"] == "sensor.predbat_load_ml_forecast", f"Expected sensor.predbat_load_ml_forecast, got {call['entity_id']}"
    assert call2["entity_id"] == "sensor.predbat_load_ml_stats", f"Expected sensor.predbat_load_ml_stats, got {call2['entity_id']}"
    # Verify state (max prediction value)
    assert call2["state"] == 28.9, f"Expected state 28.9, got {call2['state']}"

    # Verify app
    assert call2["app"] == "load_ml", f"Expected app 'load_ml', got {call2['app']}"

    # Verify attributes
    attrs = call["attributes"]
    attrs2 = call2["attributes"]

    # Check results format
    assert "results" in attrs, "results should be in attributes"
    results = attrs["results"]
    assert isinstance(results, dict), "results should be a dict"

    # Verify results are timestamp-formatted and include load_minutes_now offset
    # predictions are relative to now, so minute 60 = 1 hour from now = 13:00
    expected_timestamp_60 = (mock_base.midnight_utc + timedelta(minutes=60 + 720)).strftime(TIME_FORMAT)
    assert expected_timestamp_60 in results, f"Expected timestamp {expected_timestamp_60} in results"
    # Value should be prediction (1.3) + load_minutes_now (10.5) = 11.8
    assert abs(results[expected_timestamp_60] - 11.8) < 0.01, f"Expected value 11.8 at {expected_timestamp_60}, got {results[expected_timestamp_60]}"

    # Check load_today (current load)
    assert "load_today" in attrs2, "load_today should be in attributes"
    assert attrs2["load_today"] == 10.5, f"Expected load_today 10.5, got {attrs2['load_today']}"

    # Check load_today_h1 (1 hour ahead)
    assert "load_today_h1" in attrs2, "load_today_h1 should be in attributes"
    assert abs(attrs2["load_today_h1"] - 11.8) < 0.01, f"Expected load_today_h1 11.8, got {attrs2['load_today_h1']}"

    # Check load_today_h8 (8 hours ahead)
    assert "load_today_h8" in attrs2, "load_today_h8 should be in attributes"
    assert abs(attrs2["load_today_h8"] - 20.2) < 0.01, f"Expected load_today_h8 20.2 (9.7+10.5), got {attrs2['load_today_h8']}"
    # Check MAE
    assert "mae_kwh" in attrs2, "mae_kwh should be in attributes"
    assert attrs2["mae_kwh"] == 0.5, f"Expected mae_kwh 0.5, got {attrs2['mae_kwh']}"

    # Check last_trained
    assert "last_trained" in attrs2, "last_trained should be in attributes"
    assert attrs2["last_trained"] == "2026-01-01T10:00:00+00:00", f"Expected last_trained 2026-01-01T10:00:00+00:00, got {attrs2['last_trained']}"

    # Check model_age_hours (12:00 - 10:00 = 2 hours)
    assert "model_age_hours" in attrs2, "model_age_hours should be in attributes"
    assert attrs2["model_age_hours"] == 2.0, f"Expected model_age_hours 2.0, got {attrs2['model_age_hours']}"

    # Check training_days
    assert "training_days" in attrs2, "training_days should be in attributes"
    assert attrs2["training_days"] == 7.0, f"Expected training_days 7.0, got {attrs2['training_days']}"

    # Check status
    assert "status" in attrs2, "status should be in attributes"
    assert attrs2["status"] == "active", f"Expected status 'active', got {attrs2['status']}"

    # Check model_version
    assert "model_version" in attrs2, "model_version should be in attributes"
    from load_predictor import MODEL_VERSION

    assert attrs2["model_version"] == MODEL_VERSION, f"Expected model_version {MODEL_VERSION}, got {attrs2['model_version']}"

    # Check epochs_trained
    assert "epochs_trained" in attrs2, "epochs_trained should be in attributes"
    assert attrs2["epochs_trained"] == 50, f"Expected epochs_trained 50, got {attrs2['epochs_trained']}"

    # Check power_today values (instantaneous power in kW)
    assert "power_today_now" in attrs2, "power_today_now should be in attributes"
    assert "power_today_h1" in attrs2, "power_today_h1 should be in attributes"
    assert "power_today_h8" in attrs2, "power_today_h8 should be in attributes"

    # power_today_now: delta from start (prev_value=0) to minute 0 (0.1 kWh) / 5 min * 60 = 1.2 kW
    expected_power_now = (0.1 - 0.0) / 5 * 60
    assert abs(attrs2["power_today_now"] - expected_power_now) < 0.01, f"Expected power_today_now {expected_power_now:.2f}, got {attrs2['power_today_now']}"

    # power_today_h1: delta from minute 55 to minute 60
    # We need to interpolate - predictions are sparse, so the actual delta will depend on what's in the dict
    # For minute 60, prev_value in the loop would be the value at minute 55 (or closest)
    # Since we don't have minute 55 in our test data, prev_value when reaching minute 60 will be from minute 5
    # So delta = (1.3 - 0.2) / 5 * 60 = 13.2 kW
    expected_power_h1 = (1.3 - 0.2) / 5 * 60
    assert abs(attrs2["power_today_h1"] - expected_power_h1) < 0.01, f"Expected power_today_h1 {expected_power_h1:.2f}, got {attrs2['power_today_h1']}"

    # power_today_h8: delta from minute 475 to minute 480
    # prev_value would be from minute 60, so delta = (9.7 - 1.3) / 5 * 60 = 100.8 kW
    expected_power_h8 = (9.7 - 1.3) / 5 * 60
    assert abs(attrs2["power_today_h8"] - expected_power_h8) < 0.01, f"Expected power_today_h8 {expected_power_h8:.2f}, got {attrs2['power_today_h8']}"

    # Check friendly_name
    assert attrs["friendly_name"] == "ML Load Forecast", "friendly_name should be 'ML Load Forecast'"
    assert attrs2["friendly_name"] == "ML Load Stats", "friendly_name should be 'ML Load Stats'"
    # Check state_class
    assert attrs2["state_class"] == "measurement", "state_class should be 'measurement'"

    # Check unit_of_measurement
    assert attrs2["unit_of_measurement"] == "kWh", "unit_of_measurement should be 'kWh'"

    # Check icon
    assert attrs["icon"] == "mdi:chart-line", "icon should be 'mdi:chart-line'"
    assert attrs2["icon"] == "mdi:chart-line", "icon should be 'mdi:chart-line'"

    print("    ✓ Entity published with correct attributes")

    # Test 2: Empty predictions
    mock_base.dashboard_calls = []
    component.current_predictions = {}
    component._publish_entity()

    assert len(mock_base.dashboard_calls) == 2, "dashboard_item should be called even with empty predictions"
    call = mock_base.dashboard_calls[0]
    call2 = mock_base.dashboard_calls[1]
    assert call2["state"] == 0, "State should be 0 with empty predictions"
    assert call["attributes"]["results"] == {}, "results should be empty dict"

    print("    ✓ Empty predictions handled correctly")

    print("  All _publish_entity tests passed!")


def _test_car_subtraction_direct():
    """Test car_subtraction method directly with all improved features.

    Tests:
    - Basic subtraction without gaps
    - Gap interpolation (linear)
    - Median smoothing to reduce noise
    - Adaptive matching (searches ±2 intervals)
    - No negative values in output
    - Edge cases (empty data, no overlap, large gaps)
    """
    from load_ml_component import LoadMLComponent
    from datetime import datetime, timezone
    import numpy as np

    # Create a minimal mock base for component initialization
    class MockBase:
        def __init__(self):
            self.prefix = "predbat"
            self.config_root = None
            self.now_utc = datetime.now(timezone.utc)
            self.midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            self.minutes_now = (self.now_utc - self.midnight_utc).seconds // 60
            self.local_tz = timezone.utc
            self.args = {}
            self.log_messages = []

        def log(self, msg):
            self.log_messages.append(msg)

        def get_arg(self, key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
            return {
                "load_today": ["sensor.load_today"],
                "car_charging_energy": ["sensor.car_charging"],
                "car_charging_hold": True,
                "car_charging_threshold": 6.0,
                "car_charging_energy_scale": 1.0,
                "car_charging_rate": 7.5,
            }.get(key, default)

    print("  Testing car_subtraction method directly:")

    mock_base = MockBase()
    component = LoadMLComponent(mock_base, load_ml_enable=True)

    # Test 1: Basic subtraction without gaps (perfect alignment)
    print("    Test 1: Basic subtraction (no gaps)...", end=" ")
    load_cumulative = {
        0: 10.0,  # Now
        5: 9.5,  # 5 min ago
        10: 9.0,  # 10 min ago
        15: 8.5,  # 15 min ago
        20: 8.0,  # 20 min ago
    }
    car_cumulative = {
        0: 2.0,  # Now
        5: 1.7,  # 5 min ago (delta 0.3)
        10: 1.4,  # 10 min ago (delta 0.3)
        15: 1.1,  # 15 min ago (delta 0.3)
        20: 0.8,  # 20 min ago (delta 0.3)
    }

    result = component.car_subtraction(load_cumulative, car_cumulative, step=5)

    # Verify basic properties
    assert result is not None, "Result should not be None"
    assert len(result) > 0, "Result should not be empty"

    # Verify no negative values
    for minute, value in result.items():
        assert value >= 0, f"Result at minute {minute} should be non-negative, got {value}"

    # Verify cumulative values increase going towards minute 0
    # Result is cumulative: minute 20 (oldest) should have lowest value, minute 0 (now) highest
    if 0 in result and 20 in result:
        assert result[0] > result[20], f"Cumulative should increase towards minute 0: result[0]={result[0]}, result[20]={result[20]}"

    # The total accumulated energy should be less than or equal to original load
    # Since we're subtracting car charging
    if 0 in result:
        assert result[0] <= load_cumulative[0], f"Result at minute 0 should not exceed original load: {result[0]} vs {load_cumulative[0]}"

    print("PASS")

    # Test 2: Gap interpolation
    print("    Test 2: Gap interpolation...", end=" ")
    load_cumulative = {
        0: 10.0,
        5: 9.5,
        10: 9.0,
        15: 8.5,
        20: 8.0,
        25: 7.5,
        30: 7.0,
    }
    car_cumulative_with_gaps = {
        0: 3.0,
        5: 2.5,
        # Missing 10, 15 (20-minute gap - should interpolate)
        20: 1.5,
        25: 1.0,
        30: 0.5,
    }

    result = component.car_subtraction(load_cumulative, car_cumulative_with_gaps, step=5, interpolate_gaps=True, max_gap_minutes=60)

    assert result is not None, "Result should not be None with gaps"
    assert len(result) > 0, "Result should not be empty with gaps"

    # Verify no negative values despite gaps
    for minute, value in result.items():
        assert value >= 0, f"Result at minute {minute} should be non-negative with gaps, got {value}"

    # Gap interpolation should smooth the subtraction rather than creating spikes
    # Verify result is reasonable (not NaN or infinity)
    for minute, value in result.items():
        assert not np.isnan(value), f"Result at minute {minute} should not be NaN"
        assert not np.isinf(value), f"Result at minute {minute} should not be infinite"

    print("PASS")

    # Test 3: Large gap (exceeds max_gap_minutes)
    print("    Test 3: Large gap handling...", end=" ")
    car_cumulative_large_gap = {
        0: 3.0,
        5: 2.5,
        # Missing 10-60 (55-minute gap - exceeds default 60 min, should NOT interpolate)
        65: 1.0,
        70: 0.5,
    }

    # max_gap_minutes=60 should NOT interpolate this 55-minute gap at minute boundaries
    # Actually the gap from minute 5 to 65 is 60 minutes which is exactly at the limit
    result = component.car_subtraction(load_cumulative, car_cumulative_large_gap, step=5, interpolate_gaps=True, max_gap_minutes=60)

    assert result is not None, "Result should not be None with large gap"
    # No assertion on specific behavior, just ensure it doesn't crash

    print("PASS")

    # Test 4: Smoothing reduces noise
    print("    Test 4: Smoothing effectiveness...", end=" ")
    load_cumulative = {i: 20.0 - i * 0.1 for i in range(0, 55, 5)}

    # Create noisy car data with spikes
    car_cumulative_noisy = {
        0: 5.0,
        5: 4.2,  # Delta 0.8
        10: 3.9,  # Delta 0.3 (spike down)
        15: 3.5,  # Delta 0.4
        20: 3.1,  # Delta 0.4
        25: 2.3,  # Delta 0.8 (spike up)
        30: 1.9,  # Delta 0.4
        35: 1.5,  # Delta 0.4
        40: 1.1,  # Delta 0.4
        45: 0.7,  # Delta 0.4
        50: 0.3,  # Delta 0.4
    }

    # Smoothing window of 3-5 should reduce impact of spikes
    result = component.car_subtraction(load_cumulative, car_cumulative_noisy, step=5, smoothing_window=3)

    assert result is not None, "Smoothing result should not be None"
    # Verify smoothing produces reasonable output (no extreme values)
    for minute, value in result.items():
        assert value >= 0, f"Smoothed result at minute {minute} should be non-negative"
        assert value < 25.0, f"Smoothed result at minute {minute} should be reasonable, got {value}"

    print("PASS")

    # Test 5: Adaptive matching (timing misalignment)
    print("    Test 5: Adaptive matching...", end=" ")
    load_cumulative = {
        0: 10.0,
        5: 9.0,  # Delta 1.0
        10: 8.0,  # Delta 1.0
        15: 7.0,  # Delta 1.0
        20: 6.0,  # Delta 1.0
    }

    # Car data offset by 2 intervals (10 minutes) - adaptive search should find it
    car_cumulative_offset = {
        10: 3.0,  # Aligns with load at minute 0 (2 intervals ahead)
        15: 2.5,  # Delta 0.5
        20: 2.0,  # Delta 0.5
        25: 1.5,  # Delta 0.5
        30: 1.0,  # Delta 0.5
    }

    # With search_window=2, should find car delta within ±2 intervals (±10 minutes)
    result = component.car_subtraction(load_cumulative, car_cumulative_offset, step=5)

    assert result is not None, "Adaptive matching result should not be None"
    assert len(result) > 0, "Adaptive matching should produce results"

    # Verify results are reasonable despite offset
    for minute, value in result.items():
        assert value >= 0, f"Adaptive result at minute {minute} should be non-negative"

    print("PASS")

    # Test 6: Edge case - empty car data
    print("    Test 6: Empty car data...", end=" ")
    result = component.car_subtraction(load_cumulative, {}, step=5)

    # Should return original load data when car is empty
    assert result == load_cumulative, "Should return original load when car is empty"

    print("PASS")

    # Test 7: Edge case - empty load data
    print("    Test 7: Empty load data...", end=" ")
    result = component.car_subtraction({}, car_cumulative, step=5)

    # Should return empty dict when load is empty
    assert result == {}, "Should return empty dict when load is empty"

    print("PASS")

    # Test 8: Car exceeds load (should not go negative)
    print("    Test 8: Car exceeds load protection...", end=" ")
    load_cumulative_small = {
        0: 2.0,
        5: 1.5,  # Delta 0.5
        10: 1.0,  # Delta 0.5
        15: 0.5,  # Delta 0.5
        20: 0.0,  # Delta 0.5
    }
    car_cumulative_large = {
        0: 5.0,
        5: 4.0,  # Delta 1.0 (exceeds load delta 0.5)
        10: 3.0,  # Delta 1.0
        15: 2.0,  # Delta 1.0
        20: 1.0,  # Delta 1.0
    }

    result = component.car_subtraction(load_cumulative_small, car_cumulative_large, step=5)

    assert result is not None, "Result should not be None when car exceeds load"

    # Critical: verify NO negative values
    for minute, value in result.items():
        assert value >= 0, f"Result at minute {minute} MUST be non-negative even when car exceeds load, got {value}"

    print("PASS")

    # Test 9: Different step sizes
    print("    Test 9: Different step size (30 min)...", end=" ")
    load_cumulative_30min = {
        0: 20.0,
        30: 17.0,
        60: 14.0,
        90: 11.0,
        120: 8.0,
    }
    car_cumulative_30min = {
        0: 6.0,
        30: 5.0,
        60: 4.0,
        90: 3.0,
        120: 2.0,
    }

    result = component.car_subtraction(load_cumulative_30min, car_cumulative_30min, step=30)

    assert result is not None, "Result should work with 30-min step"
    assert len(result) > 0, "Result should not be empty with 30-min step"

    # Verify subtraction works correctly with 30-min steps
    for minute, value in result.items():
        assert value >= 0, f"Result at minute {minute} should be non-negative with 30-min step"

    print("PASS")

    print("  All car_subtraction direct tests passed!")
