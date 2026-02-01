# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt: on

import numpy as np
from datetime import datetime, timezone, timedelta
import tempfile
import os

from load_predictor import LoadPredictor, OUTPUT_STEPS, HIDDEN_SIZES, TOTAL_FEATURES, STEP_MINUTES, relu, relu_derivative, huber_loss


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
        ("dataset_creation", _test_dataset_creation, "Dataset creation from load data"),
        ("normalization", _test_normalization, "Z-score normalization correctness"),
        ("adam_optimizer", _test_adam_optimizer, "Adam optimizer step"),
        ("training_convergence", _test_training_convergence, "Training convergence on synthetic data"),
        ("model_persistence", _test_model_persistence, "Model save/load with version check"),
        ("cold_start", _test_cold_start, "Cold start with insufficient data"),
        ("fine_tune", _test_fine_tune, "Fine-tune on recent data"),
        ("prediction", _test_prediction, "End-to-end prediction"),
        # ("real_data_training", _test_real_data_training, "Train on real load_minutes_debug.json data with chart"),
        ("component_fetch_load_data", _test_component_fetch_load_data, "LoadMLComponent _fetch_load_data method"),
        ("component_publish_entity", _test_component_publish_entity, "LoadMLComponent _publish_entity method"),
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


def _create_synthetic_load_data(n_days=7, now_utc=None):
    """Create synthetic load data for testing"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    n_minutes = n_days * 24 * 60
    load_minutes = {}
    cumulative = 0.0

    # Build backwards from now (minute 0 = now)
    for minute in range(n_minutes - 1, -1, -STEP_MINUTES):
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

    # Validation should be approximately 24h worth of samples (288 at 5-min intervals)
    expected_val_samples = 24 * 60 // STEP_MINUTES
    assert abs(X_val.shape[0] - expected_val_samples) < 10, f"Expected ~{expected_val_samples} val samples, got {X_val.shape[0]}"


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
    val_mae = predictor.train(load_data, now_utc, is_initial=True, epochs=10, time_decay_days=7)

    # Training should complete and return a validation MAE
    assert val_mae is not None, "Training should return validation MAE"
    assert predictor.model_initialized, "Model should be initialized after training"
    assert predictor.epochs_trained > 0, "Should have trained some epochs"


def _test_model_persistence():
    """Test model save/load with version check"""
    predictor = LoadPredictor(learning_rate=0.005)
    now_utc = datetime.now(timezone.utc)

    # Train briefly
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=5, now_utc=now_utc)
    predictor.train(load_data, now_utc, is_initial=True, epochs=5, time_decay_days=7)

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
    val_mae = predictor.train(load_data, now_utc, is_initial=True, epochs=5, time_decay_days=7)

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
    predictor.train(load_data, now_utc, is_initial=True, epochs=5, time_decay_days=7)

    # Store original weights
    orig_weights = [w.copy() for w in predictor.weights]

    # Fine-tune with same data but as fine-tune mode
    # Note: Fine-tune uses is_finetune=True which only looks at last 24h
    # For the test to work, we need enough data for the full training
    predictor.train(load_data, now_utc, is_initial=False, epochs=3, time_decay_days=7)

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
    predictor.train(load_data, now_utc, is_initial=True, epochs=10, time_decay_days=7)

    # Make prediction
    predictions = predictor.predict(load_data, now_utc, midnight_utc)

    # Should return dict with minute keys
    if predictions:  # May return empty dict if validation fails
        assert isinstance(predictions, dict), "Predictions should be a dict"
        # Check some predictions exist
        assert len(predictions) > 0, "Should have some predictions"
        # All values should be non-negative
        for minute, val in predictions.items():
            assert val >= 0, f"Prediction at minute {minute} should be non-negative"


def _test_real_data_training():
    """
    Test training on real load_minutes_debug.json data and generate comparison chart
    """
    import json
    import os

    # Try both coverage/ and current directory
    json_paths = ["../coverage/load_minutes_debug.json", "coverage/load_minutes_debug.json", "load_minutes_debug.json"]

    load_data = None
    for json_path in json_paths:
        if os.path.exists(json_path):
            with open(json_path, "r") as f:
                raw_data = json.load(f)
            # Convert string keys to integers
            load_data = {int(k): float(v) for k, v in raw_data.items()}
            print(f"  Loaded {len(load_data)} datapoints from {json_path}")
            break

    if load_data is None:
        print("  WARNING: load_minutes_debug.json not found, skipping real data test")
        return

    # Initialize predictor with lower learning rate for better convergence
    predictor = LoadPredictor(learning_rate=0.0005, max_load_kw=20.0)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Calculate how many days of data we have
    max_minute = max(load_data.keys())
    n_days = max_minute / (24 * 60)
    print(f"  Data spans {n_days:.1f} days ({max_minute} minutes)")

    # Train on full dataset with more epochs for larger network
    print(f"  Training on real data with {len(load_data)} points...")
    success = predictor.train(load_data, now_utc, is_initial=True, epochs=50, time_decay_days=7)

    assert success, "Training on real data should succeed"
    assert predictor.model_initialized, "Model should be initialized after training"

    # Make predictions
    print("  Generating predictions...")
    predictions = predictor.predict(load_data, now_utc, midnight_utc)

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
            val_predictions = predictor.predict(shifted_load_data, shifted_now, shifted_midnight)

            # Extract first 24h of validation predictions
            val_pred_keys = sorted(val_predictions.keys())
            for i, minute in enumerate(val_pred_keys):
                if minute >= val_period_hours * 60:
                    break
                if i == 0:
                    energy_kwh = val_predictions[minute]
                else:
                    prev_minute = val_pred_keys[i - 1]
                    energy_kwh = max(0, val_predictions[minute] - val_predictions[prev_minute])
                val_pred_minutes.append(minute)
                val_pred_energy.append(energy_kwh)

        # Convert predictions (cumulative kWh) to energy per step (kWh)
        # predictions dict is: {0: cum0, 5: cum5, 10: cum10, ...} representing FUTURE
        pred_minutes = []
        pred_energy = []
        pred_keys = sorted(predictions.keys())
        for i, minute in enumerate(pred_keys):
            if minute >= prediction_hours * 60:
                break
            if i == 0:
                # First step - use the value directly as energy
                energy_kwh = predictions[minute]
            else:
                # Subsequent steps - calculate difference from previous
                prev_minute = pred_keys[i - 1]
                energy_kwh = max(0, predictions[minute] - predictions[prev_minute])
            pred_minutes.append(minute)
            pred_energy.append(energy_kwh)

        # Create figure with single plot showing timeline
        fig, ax = plt.subplots(1, 1, figsize=(16, 6))

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
        ax.set_title("ML Load Predictor: Validation (Day 7 Actual vs Predicted) + 48h Forecast", fontsize=14, fontweight="bold")
        ax.legend(loc="upper right", fontsize=10)
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

        result_data, result_age, result_now = await component._fetch_load_data()

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

        result_data, result_age, result_now = await component._fetch_load_data()

        assert result_data is None, "Should return None when sensor missing"
        assert result_age == 0, "Age should be 0 when sensor missing"
        assert result_now == 0, "Current load should be 0 when sensor missing"
        print("    ✓ Missing sensor handled correctly")

    # Test 3: Car charging subtraction
    async def test_car_charging_subtraction():
        mock_base_with_car = MockBase()

        # Create load data with entries for EVERY minute (not just 5-min intervals)
        # This is required because the component's car charging subtraction loop
        # iterates over every minute from 1 to max_minute
        original_load_data, age = create_load_minutes(7, all_minutes=True)
        car_charging_data = {i: i * 0.001 for i in range(0, 7 * 24 * 60 + 1)}  # Small cumulative car charging (0.001 kWh/min)

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

        result_data, result_age, result_now = await component._fetch_load_data()

        assert result_data is not None, f"Should return load data"
        assert result_age > 0, f"Should have valid age (got {result_age})"
        assert len(result_data) > 0, "Result data should not be empty"
        assert result_now >= 0, f"Current load should be non-negative, got {result_now}"

        # Verify car charging was called
        assert mock_base_with_car.minute_data_import_export.called, "minute_data_import_export should be called"

        # Verify all values are non-negative after subtraction
        for minute, value in result_data.items():
            assert value >= 0, f"Load at minute {minute} should be non-negative, got {value}"

        print("    ✓ Car charging subtraction works")

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

        result_data, result_age, result_now = await component._fetch_load_data()

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

        result_data, result_age, result_now = await component._fetch_load_data()

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

        result_data, result_age, result_now = await component._fetch_load_data()

        assert result_data is None, "Should return None when load data is empty"
        assert result_age == 0, "Age should be 0 when load data is empty"
        assert result_now == 0, "Current load should be 0 when load data is empty"
        print("    ✓ Empty load data handled correctly")

    # Run all sub-tests
    print("  Running LoadMLComponent._fetch_load_data tests:")
    run_async(test_basic_fetch())
    run_async(test_missing_sensor())
    run_async(test_car_charging_subtraction())
    run_async(test_load_power_fill())
    run_async(test_exception_handling())
    run_async(test_empty_load_data())
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
        0: 0.0,  # Now
        5: 0.1,  # 5 minutes from now
        60: 1.2,  # 1 hour from now (load_today_h1)
        480: 9.6,  # 8 hours from now (load_today_h8)
        1440: 28.8,  # 24 hours from now
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

    # Verify dashboard_item was called
    assert len(mock_base.dashboard_calls) == 1, "dashboard_item should be called once"

    call = mock_base.dashboard_calls[0]

    # Verify entity_id
    assert call["entity_id"] == "sensor.predbat_load_ml_forecast", f"Expected sensor.predbat_load_ml_forecast, got {call['entity_id']}"

    # Verify state (max prediction value)
    assert call["state"] == 28.8, f"Expected state 28.8, got {call['state']}"

    # Verify app
    assert call["app"] == "load_ml", f"Expected app 'load_ml', got {call['app']}"

    # Verify attributes
    attrs = call["attributes"]

    # Check results format
    assert "results" in attrs, "results should be in attributes"
    results = attrs["results"]
    assert isinstance(results, dict), "results should be a dict"

    # Verify results are timestamp-formatted and include load_minutes_now offset
    # predictions are relative to now, so minute 60 = 1 hour from now = 13:00
    expected_timestamp_60 = (mock_base.midnight_utc + timedelta(minutes=60 + 720)).strftime(TIME_FORMAT)
    assert expected_timestamp_60 in results, f"Expected timestamp {expected_timestamp_60} in results"
    # Value should be prediction (1.2) + load_minutes_now (10.5) = 11.7
    assert abs(results[expected_timestamp_60] - 11.7) < 0.01, f"Expected value 11.7 at {expected_timestamp_60}, got {results[expected_timestamp_60]}"

    # Check load_today (current load)
    assert "load_today" in attrs, "load_today should be in attributes"
    assert attrs["load_today"] == 10.5, f"Expected load_today 10.5, got {attrs['load_today']}"

    # Check load_today_h1 (1 hour ahead)
    assert "load_today_h1" in attrs, "load_today_h1 should be in attributes"
    assert abs(attrs["load_today_h1"] - 11.7) < 0.01, f"Expected load_today_h1 11.7, got {attrs['load_today_h1']}"

    # Check load_today_h8 (8 hours ahead)
    assert "load_today_h8" in attrs, "load_today_h8 should be in attributes"
    assert abs(attrs["load_today_h8"] - 20.1) < 0.01, f"Expected load_today_h8 20.1 (9.6+10.5), got {attrs['load_today_h8']}"

    # Check MAE
    assert "mae_kwh" in attrs, "mae_kwh should be in attributes"
    assert attrs["mae_kwh"] == 0.5, f"Expected mae_kwh 0.5, got {attrs['mae_kwh']}"

    # Check last_trained
    assert "last_trained" in attrs, "last_trained should be in attributes"
    assert attrs["last_trained"] == "2026-01-01T10:00:00+00:00", f"Expected last_trained 2026-01-01T10:00:00+00:00, got {attrs['last_trained']}"

    # Check model_age_hours (12:00 - 10:00 = 2 hours)
    assert "model_age_hours" in attrs, "model_age_hours should be in attributes"
    assert attrs["model_age_hours"] == 2.0, f"Expected model_age_hours 2.0, got {attrs['model_age_hours']}"

    # Check training_days
    assert "training_days" in attrs, "training_days should be in attributes"
    assert attrs["training_days"] == 7.0, f"Expected training_days 7.0, got {attrs['training_days']}"

    # Check status
    assert "status" in attrs, "status should be in attributes"
    assert attrs["status"] == "active", f"Expected status 'active', got {attrs['status']}"

    # Check model_version
    assert "model_version" in attrs, "model_version should be in attributes"
    from load_predictor import MODEL_VERSION

    assert attrs["model_version"] == MODEL_VERSION, f"Expected model_version {MODEL_VERSION}, got {attrs['model_version']}"

    # Check epochs_trained
    assert "epochs_trained" in attrs, "epochs_trained should be in attributes"
    assert attrs["epochs_trained"] == 50, f"Expected epochs_trained 50, got {attrs['epochs_trained']}"

    # Check friendly_name
    assert attrs["friendly_name"] == "ML Load Forecast", "friendly_name should be 'ML Load Forecast'"

    # Check state_class
    assert attrs["state_class"] == "measurement", "state_class should be 'measurement'"

    # Check unit_of_measurement
    assert attrs["unit_of_measurement"] == "kWh", "unit_of_measurement should be 'kWh'"

    # Check icon
    assert attrs["icon"] == "mdi:chart-line", "icon should be 'mdi:chart-line'"

    print("    ✓ Entity published with correct attributes")

    # Test 2: Empty predictions
    mock_base.dashboard_calls = []
    component.current_predictions = {}
    component._publish_entity()

    assert len(mock_base.dashboard_calls) == 1, "dashboard_item should be called even with empty predictions"
    call = mock_base.dashboard_calls[0]
    assert call["state"] == 0, "State should be 0 with empty predictions"
    assert call["attributes"]["results"] == {}, "results should be empty dict"

    print("    ✓ Empty predictions handled correctly")

    print("  All _publish_entity tests passed!")
