# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Lightweight ML Load Predictor - NumPy-only MLP implementation
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""NumPy-only MLP neural network for household load forecasting.

Implements a 4-layer feed-forward network (512-256-128-64) trained via
Adam optimiser with autoregressive prediction for 48-hour load forecasts.
Uses historical load, PV generation, temperature, and energy rates as
input features with cyclical time encoding.
"""

import numpy as np
import json
import os
from datetime import datetime, timezone, timedelta

# Architecture constants (not user-configurable)
MODEL_VERSION = 7  # Bumped due to bug fixes in input data, should be retrained - 17/2/26
LOOKBACK_STEPS = 288  # 24 hours at 5-min intervals
OUTPUT_STEPS = 1  # Single step output (autoregressive)
PREDICT_HORIZON = 576  # 48 hours of predictions (576 * 5 min)
HIDDEN_SIZES = [512, 256, 128, 64]  # Deeper network with more capacity
BATCH_SIZE = 128  # Smaller batches for better gradient estimates
STEP_MINUTES = 5  # Minutes per step

# Feature constants
NUM_TIME_FEATURES = 4  # sin/cos minute-of-day, sin/cos day-of-week (for TARGET time)
NUM_LOAD_FEATURES = LOOKBACK_STEPS  # Historical load values
NUM_PV_FEATURES = LOOKBACK_STEPS  # Historical PV generation values
NUM_TEMP_FEATURES = LOOKBACK_STEPS  # Historical temperature values
NUM_IMPORT_RATE_FEATURES = LOOKBACK_STEPS  # Historical import rates
NUM_EXPORT_RATE_FEATURES = LOOKBACK_STEPS  # Historical export rates
TOTAL_FEATURES = NUM_LOAD_FEATURES + NUM_PV_FEATURES + NUM_TEMP_FEATURES + NUM_IMPORT_RATE_FEATURES + NUM_EXPORT_RATE_FEATURES + NUM_TIME_FEATURES


def relu(x):
    """ReLU activation function"""
    return np.maximum(0, x)


def relu_derivative(x):
    """Derivative of ReLU"""
    return (x > 0).astype(np.float32)


def huber_loss(y_true, y_pred, delta=1.0):
    """Huber loss - robust to outliers"""
    error = y_true - y_pred
    abs_error = np.abs(error)
    quadratic = np.minimum(abs_error, delta)
    linear = abs_error - quadratic
    return np.mean(0.5 * quadratic**2 + delta * linear)


def huber_loss_derivative(y_true, y_pred, delta=1.0):
    """Derivative of Huber loss"""
    error = y_pred - y_true
    abs_error = np.abs(error)
    return np.where(abs_error <= delta, error, delta * np.sign(error)) / y_true.shape[0]


def mse_loss(y_true, y_pred):
    """Mean Squared Error loss"""
    return np.mean((y_true - y_pred) ** 2)


def mse_loss_derivative(y_true, y_pred):
    """Derivative of MSE loss"""
    return 2 * (y_pred - y_true) / y_true.shape[0]


class LoadPredictor:
    """
    Lightweight MLP-based load predictor using NumPy only.

    Predicts household electrical load for the next 48 hours using:
    - Historical load data (lookback window)
    - Cyclical time encodings (hour-of-day, day-of-week)
    - Placeholder for future exogenous features (temperature, solar)
    """

    def __init__(self, log_func=None, learning_rate=0.001, max_load_kw=23.0, weight_decay=0.01):
        """
        Initialize the load predictor.

        Args:
            log_func: Logging function (defaults to print)
            learning_rate: Learning rate for Adam optimizer
            max_load_kw: Maximum load in kW for clipping predictions
            weight_decay: L2 regularization coefficient for AdamW (0.0 disables)
        """
        self.log = log_func if log_func else print
        self.learning_rate = learning_rate
        self.max_load_kw = max_load_kw
        self.weight_decay = weight_decay

        # Model weights (initialized on first train)
        self.weights = None
        self.biases = None

        # Adam optimizer state
        self.m_weights = None
        self.v_weights = None
        self.m_biases = None
        self.v_biases = None
        self.adam_t = 0

        # Normalization parameters
        self.feature_mean = None
        self.feature_std = None
        self.target_mean = None
        self.target_std = None
        self.pv_mean = None
        self.pv_std = None

        # Training metadata
        self.training_timestamp = None
        self.validation_mae = None
        self.epochs_trained = 0
        self.model_initialized = False

    def _initialize_weights(self):
        """Initialize network weights using He initialization (optimal for ReLU)"""
        np.random.seed(42)  # For reproducibility

        layer_sizes = [TOTAL_FEATURES] + HIDDEN_SIZES + [OUTPUT_STEPS]

        self.weights = []
        self.biases = []
        self.m_weights = []
        self.v_weights = []
        self.m_biases = []
        self.v_biases = []

        for i in range(len(layer_sizes) - 1):
            fan_in = layer_sizes[i]
            fan_out = layer_sizes[i + 1]

            # He initialization (optimal for ReLU activations)
            std = np.sqrt(2.0 / fan_in)
            w = np.random.randn(fan_in, fan_out).astype(np.float32) * std
            b = np.zeros(fan_out, dtype=np.float32)

            self.weights.append(w)
            self.biases.append(b)

            # Adam optimizer momentum terms
            self.m_weights.append(np.zeros_like(w))
            self.v_weights.append(np.zeros_like(w))
            self.m_biases.append(np.zeros_like(b))
            self.v_biases.append(np.zeros_like(b))

        self.adam_t = 0
        self.model_initialized = True

    def _reset_adam_optimizer(self):
        """
        Reset Adam optimizer momentum to zero.

        Used when starting fine-tuning to prevent accumulated momentum
        from previous training sessions causing overfitting on small
        fine-tuning datasets.
        """
        if not self.model_initialized:
            return

        for i in range(len(self.weights)):
            self.m_weights[i] = np.zeros_like(self.weights[i])
            self.v_weights[i] = np.zeros_like(self.weights[i])
            self.m_biases[i] = np.zeros_like(self.biases[i])
            self.v_biases[i] = np.zeros_like(self.biases[i])

        self.adam_t = 0
        self.log("ML Predictor: Reset Adam optimizer state for fine-tuning")

    def _forward(self, X):
        """
        Forward pass through the network.

        Args:
            X: Input features (batch_size, TOTAL_FEATURES)

        Returns:
            Output predictions and list of layer activations for backprop
        """
        activations = [X]
        pre_activations = []

        current = X
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            z = np.dot(current, w) + b
            pre_activations.append(z)

            # Apply ReLU for hidden layers, linear for output
            if i < len(self.weights) - 1:
                current = relu(z)
            else:
                current = z  # Linear output

            activations.append(current)

        return current, activations, pre_activations

    def _backward(self, y_true, activations, pre_activations, sample_weights=None):
        """
        Backward pass using backpropagation.

        Args:
            y_true: True target values
            activations: Layer activations from forward pass
            pre_activations: Pre-activation values from forward pass
            sample_weights: Optional per-sample weights for weighted loss

        Returns:
            Gradients for weights and biases
        """
        batch_size = y_true.shape[0]

        # Output layer gradient (MSE loss derivative)
        delta = mse_loss_derivative(y_true, activations[-1])

        # Apply sample weights to gradient if provided
        if sample_weights is not None:
            delta = delta * sample_weights.reshape(-1, 1)

        weight_grads = []
        bias_grads = []

        # Backpropagate through layers
        for i in range(len(self.weights) - 1, -1, -1):
            # Gradient for weights and biases
            weight_grads.insert(0, np.dot(activations[i].T, delta))
            bias_grads.insert(0, np.sum(delta, axis=0))

            if i > 0:
                # Propagate gradient to previous layer
                delta = np.dot(delta, self.weights[i].T) * relu_derivative(pre_activations[i - 1])

        return weight_grads, bias_grads

    def _adam_update(self, weight_grads, bias_grads, beta1=0.9, beta2=0.999, epsilon=1e-8):
        """
        Update weights using Adam optimizer with optional weight decay (AdamW).

        Args:
            weight_grads: Gradients for weights
            bias_grads: Gradients for biases
            beta1: Exponential decay rate for first moment
            beta2: Exponential decay rate for second moment
            epsilon: Small constant for numerical stability
        """
        self.adam_t += 1

        for i in range(len(self.weights)):
            # Update momentum for weights
            self.m_weights[i] = beta1 * self.m_weights[i] + (1 - beta1) * weight_grads[i]
            self.v_weights[i] = beta2 * self.v_weights[i] + (1 - beta2) * (weight_grads[i] ** 2)

            # Bias correction
            m_hat = self.m_weights[i] / (1 - beta1**self.adam_t)
            v_hat = self.v_weights[i] / (1 - beta2**self.adam_t)

            # Update weights with Adam step
            self.weights[i] -= self.learning_rate * m_hat / (np.sqrt(v_hat) + epsilon)

            # Apply weight decay (AdamW-style L2 regularization)
            if self.weight_decay > 0:
                self.weights[i] *= 1 - self.learning_rate * self.weight_decay

            # Update momentum for biases
            self.m_biases[i] = beta1 * self.m_biases[i] + (1 - beta1) * bias_grads[i]
            self.v_biases[i] = beta2 * self.v_biases[i] + (1 - beta2) * (bias_grads[i] ** 2)

            # Bias correction
            m_hat = self.m_biases[i] / (1 - beta1**self.adam_t)
            v_hat = self.v_biases[i] / (1 - beta2**self.adam_t)

            # Update biases (no weight decay on biases)
            self.biases[i] -= self.learning_rate * m_hat / (np.sqrt(v_hat) + epsilon)

    def _create_time_features(self, minute_of_day, day_of_week):
        """
        Create cyclical time features.

        Args:
            minute_of_day: Minutes since midnight (0-1439)
            day_of_week: Day of week (0-6, Monday=0)

        Returns:
            Array of 4 time features: sin/cos minute, sin/cos day
        """
        # Cyclical encoding for minute of day
        minute_sin = np.sin(2 * np.pi * minute_of_day / 1440)
        minute_cos = np.cos(2 * np.pi * minute_of_day / 1440)

        # Cyclical encoding for day of week
        day_sin = np.sin(2 * np.pi * day_of_week / 7)
        day_cos = np.cos(2 * np.pi * day_of_week / 7)

        return np.array([minute_sin, minute_cos, day_sin, day_cos], dtype=np.float32)

    def _add_exog_features(self, X, exog_dict=None):
        """
        Placeholder for adding exogenous features (temperature, solar).

        Args:
            X: Current feature array
            exog_dict: Dictionary with optional "temperature" and "solar" data

        Returns:
            Extended feature array (currently just returns X unchanged)
        """
        # Future expansion: add temperature/solar features here
        if exog_dict:
            pass  # Placeholder for future implementation
        return X

    def _load_to_energy_per_step(self, load_minutes, step=STEP_MINUTES):
        """
        Convert cumulative load_minutes dict to energy per step (kWh per 5 min).

        The load_minutes dict contains cumulative kWh values:
        - Positive minutes: going backwards in time (historical data)
        - Negative minutes: going forward in time (future forecasts)
        Energy consumption for a period is the difference between start and end.

        Args:
            load_minutes: Dict of {minute: cumulative_kwh}
            step: Step size in minutes

        Returns:
            Dict of {minute: energy_kwh_per_step}
        """
        energy_per_step = {}

        if not load_minutes:
            return energy_per_step

        # Get both positive (historical) and negative (future) minute ranges
        all_minutes = list(load_minutes.keys())
        if not all_minutes:
            return energy_per_step

        max_minute = max(all_minutes)
        min_minute = min(all_minutes)

        # Process historical data (positive minutes, going backwards)
        for minute in range(0, max_minute, step):
            # Energy = cumulative_now - cumulative_later (going backwards)
            val_now = load_minutes.get(minute, 0)
            val_next = load_minutes.get(minute + step, 0)
            energy = max(val_now - val_next, 0)  # Ensure non-negative
            energy_per_step[minute] = energy

        # Process future data (negative minutes, going forwards)
        if min_minute < 0:
            # Need to go from min_minute (-XXX) towards 0 in positive steps
            # So we go from min to 0-step in steps of +step
            for minute in range(min_minute, -step + 1, step):
                # For future: energy = cumulative_now - cumulative_later (cumulative decreases going forward)
                val_now = load_minutes.get(minute, 0)
                val_next = load_minutes.get(minute + step, 0)
                energy = max(val_now - val_next, 0)  # Ensure non-negative
                energy_per_step[minute] = energy

        return energy_per_step

    def _compute_daily_pattern(self, energy_per_step, smoothing_window=6):
        """
        Compute average daily pattern from historical data.

        Groups energy values by minute-of-day and computes rolling average.
        Used to blend with predictions to prevent autoregressive drift.

        Args:
            energy_per_step: Dict of {minute: energy_kwh}
            smoothing_window: Number of adjacent slots to smooth over

        Returns:
            Dict of {minute_of_day: avg_energy} for 288 slots in a day
        """
        # Collect energy values by minute-of-day (0 to 1435 in 5-min steps)
        by_minute = {}
        for minute, energy in energy_per_step.items():
            minute_of_day = minute % (24 * 60)  # 0-1439
            # Align to 5-minute boundaries
            slot = (minute_of_day // STEP_MINUTES) * STEP_MINUTES
            if slot not in by_minute:
                by_minute[slot] = []
            by_minute[slot].append(energy)

        # Compute mean for each slot
        pattern = {}
        for slot in range(0, 24 * 60, STEP_MINUTES):
            if slot in by_minute and len(by_minute[slot]) > 0:
                pattern[slot] = float(np.mean(by_minute[slot]))
            else:
                pattern[slot] = 0.05  # Default fallback

        # Apply smoothing to reduce noise
        slots = sorted(pattern.keys())
        smoothed = {}
        for i, slot in enumerate(slots):
            values = []
            for offset in range(-smoothing_window // 2, smoothing_window // 2 + 1):
                idx = (i + offset) % len(slots)
                values.append(pattern[slots[idx]])
            smoothed[slot] = float(np.mean(values))

        return smoothed

    def _create_dataset(self, load_minutes, now_utc, pv_minutes=None, temp_minutes=None, import_rates=None, export_rates=None, is_finetune=False, time_decay_days=7, validation_holdout_hours=24):
        """
        Create training dataset from load_minutes dict.

        For autoregressive prediction: each sample uses 24h lookback to predict
        the next single 5-minute step. Time features are for the TARGET time.

        Training uses all available data (from most recent to as far back as data goes).
        Validation uses the most recent 24h as a subset of training data to check model fit.

        Args:
            load_minutes: Dict of {minute: cumulative_kwh} going backwards in time
            now_utc: Current UTC timestamp
            pv_minutes: Dict of {minute: cumulative_kwh} PV generation (backwards for history, negative for future)
            temp_minutes: Dict of {minute: temperature_celsius} Temperature (backwards for history, negative for future)
            import_rates: Dict of {minute: rate_per_kwh} Import rates (backwards for history, negative for future)
            export_rates: Dict of {minute: rate_per_kwh} Export rates (backwards for history, negative for future)
            is_finetune: If True, only use last 24 hours; else use full data with time-decay
            time_decay_days: Time constant for exponential decay weighting
            validation_holdout_hours: Hours of most recent data to hold out for validation

        Returns:
            X_train, y_train, train_weights: Training data
            X_val, y_val: Validation data (most recent period)
        """
        # Convert to energy per step
        energy_per_step = self._load_to_energy_per_step(load_minutes)
        pv_energy_per_step = self._load_to_energy_per_step(pv_minutes) if pv_minutes else {}
        # Temperature is not cumulative, so just use the raw values (already in correct format)
        temp_values = temp_minutes if temp_minutes else {}
        # Import and export rates are not cumulative, use raw values
        import_rate_values = import_rates if import_rates else {}
        export_rate_values = export_rates if export_rates else {}

        if not energy_per_step:
            return None, None, None, None, None

        max_minute = max(energy_per_step.keys())

        # Use all data for training
        start_minute = 0
        end_minute = max_minute

        # Need enough history for lookback plus validation holdout
        min_required = LOOKBACK_STEPS * STEP_MINUTES + validation_holdout_hours * 60 + STEP_MINUTES

        if end_minute < min_required:
            self.log("Warn: Insufficient data for ML training, need {} minutes, have {}".format(min_required, end_minute))
            return None, None, None, None, None

        self.log("ML Predictor: Creating dataset with {} hours of training samples and {} hours of validation samples".format(end_minute // 60, validation_holdout_hours))

        # Validation uses most recent data (minute 0 to validation_holdout)
        # Training uses ALL data (minute 0 to end_minute), including validation period
        validation_end = validation_holdout_hours * 60

        X_train_list = []
        y_train_list = []
        weight_list = []
        X_val_list = []
        y_val_list = []

        # Create training samples (from all available data, including most recent)
        # These samples predict targets in the range [0, end_minute - lookback]
        for target_minute in range(0, end_minute - LOOKBACK_STEPS * STEP_MINUTES, STEP_MINUTES):
            # Lookback window starts at target_minute + STEP_MINUTES (one step after target)
            lookback_start = target_minute + STEP_MINUTES

            # Extract lookback window (24 hours of history before the target)
            lookback_values = []
            pv_lookback_values = []
            temp_lookback_values = []
            import_rate_lookback = []
            export_rate_lookback = []
            valid_sample = True

            for lb_offset in range(LOOKBACK_STEPS):
                lb_minute = lookback_start + lb_offset * STEP_MINUTES
                if lb_minute in energy_per_step:
                    lookback_values.append(energy_per_step[lb_minute])
                    # Add PV generation for the same time period (0 if no PV data)
                    pv_lookback_values.append(pv_energy_per_step.get(lb_minute, 0.0))
                    # Add temperature for the same time period (0 if no temp data)
                    temp_lookback_values.append(temp_values.get(lb_minute, 0.0))
                    # Add import/export rates for the same time period (0 if no rate data)
                    import_rate_lookback.append(import_rate_values.get(lb_minute, 0.0))
                    export_rate_lookback.append(export_rate_values.get(lb_minute, 0.0))
                else:
                    valid_sample = False
                    break

            if not valid_sample or len(lookback_values) != LOOKBACK_STEPS:
                continue

            # Target is the single next step we're predicting
            if target_minute not in energy_per_step:
                continue
            target_value = energy_per_step[target_minute]

            # Calculate time features for the TARGET time (what we're predicting)
            target_time = now_utc - timedelta(minutes=target_minute)
            minute_of_day = target_time.hour * 60 + target_time.minute
            day_of_week = target_time.weekday()
            time_features = self._create_time_features(minute_of_day, day_of_week)

            # Combine features: [load_lookback..., pv_lookback..., temp_lookback..., import_rates..., export_rates..., time_features...]
            features = np.concatenate(
                [
                    np.array(lookback_values, dtype=np.float32),
                    np.array(pv_lookback_values, dtype=np.float32),
                    np.array(temp_lookback_values, dtype=np.float32),
                    np.array(import_rate_lookback, dtype=np.float32),
                    np.array(export_rate_lookback, dtype=np.float32),
                    time_features,
                ]
            )

            X_train_list.append(features)
            y_train_list.append(np.array([target_value], dtype=np.float32))

            # Time-decay weighting (older samples get lower weight)
            age_days = target_minute / (24 * 60)
            weight = np.exp(-age_days / time_decay_days)
            weight_list.append(weight)

        # Create validation samples (from most recent data, minute 0 to validation_end)
        # These samples use lookback from validation_end onwards to predict the holdout period
        for target_minute in range(0, validation_end, STEP_MINUTES):
            # Lookback window starts at target_minute + STEP_MINUTES
            lookback_start = target_minute + STEP_MINUTES

            # Extract lookback window
            lookback_values = []
            pv_lookback_values = []
            temp_lookback_values = []
            import_rate_lookback = []
            export_rate_lookback = []
            valid_sample = True

            for lb_offset in range(LOOKBACK_STEPS):
                lb_minute = lookback_start + lb_offset * STEP_MINUTES
                if lb_minute in energy_per_step:
                    lookback_values.append(energy_per_step[lb_minute])
                    pv_lookback_values.append(pv_energy_per_step.get(lb_minute, 0.0))
                    temp_lookback_values.append(temp_values.get(lb_minute, 0.0))
                    import_rate_lookback.append(import_rate_values.get(lb_minute, 0.0))
                    export_rate_lookback.append(export_rate_values.get(lb_minute, 0.0))
                else:
                    valid_sample = False
                    break

            if not valid_sample or len(lookback_values) != LOOKBACK_STEPS:
                continue

            # Target value
            if target_minute not in energy_per_step:
                continue
            target_value = energy_per_step[target_minute]

            # Time features for target time
            target_time = now_utc - timedelta(minutes=target_minute)
            minute_of_day = target_time.hour * 60 + target_time.minute
            day_of_week = target_time.weekday()
            time_features = self._create_time_features(minute_of_day, day_of_week)

            features = np.concatenate(
                [
                    np.array(lookback_values, dtype=np.float32),
                    np.array(pv_lookback_values, dtype=np.float32),
                    np.array(temp_lookback_values, dtype=np.float32),
                    np.array(import_rate_lookback, dtype=np.float32),
                    np.array(export_rate_lookback, dtype=np.float32),
                    time_features,
                ]
            )

            X_val_list.append(features)
            y_val_list.append(np.array([target_value], dtype=np.float32))

        if not X_train_list:
            return None, None, None, None, None

        X_train = np.array(X_train_list, dtype=np.float32)
        y_train = np.array(y_train_list, dtype=np.float32)
        train_weights = np.array(weight_list, dtype=np.float32)

        # Normalize weights to sum to number of samples
        train_weights = train_weights * len(train_weights) / np.sum(train_weights)

        X_val = np.array(X_val_list, dtype=np.float32) if X_val_list else None
        y_val = np.array(y_val_list, dtype=np.float32) if y_val_list else None

        return X_train, y_train, train_weights, X_val, y_val

    def _get_min_std_array(self, n_features):
        """
        Return the per-feature minimum std array used to prevent extreme normalization.

        Args:
            n_features: Total number of features

        Returns:
            numpy array of minimum std values, shape (n_features,)
        """
        min_std = np.ones(n_features) * 1e-8  # Default fallback
        if n_features == TOTAL_FEATURES:
            min_std[0:LOOKBACK_STEPS] = 0.01  # Load energy (kWh)
            min_std[LOOKBACK_STEPS : 2 * LOOKBACK_STEPS] = 0.01  # PV energy (kWh)
            min_std[2 * LOOKBACK_STEPS : 3 * LOOKBACK_STEPS] = 0.5  # Temperature (°C)
            min_std[3 * LOOKBACK_STEPS : 4 * LOOKBACK_STEPS] = 1.0  # Import rates (p/kWh)
            min_std[4 * LOOKBACK_STEPS : 5 * LOOKBACK_STEPS] = 1.0  # Export rates (p/kWh)
            min_std[5 * LOOKBACK_STEPS :] = 0.01  # Time features (cyclical)
        return min_std

    def _log_normalization_stats(self, label=""):
        """
        Log per-feature-group normalization statistics for drift tracking.

        Logs mean-of-means and mean-of-stds for each feature group so the
        log file can be grepped for 'Normalization stats' to compare over time.

        Args:
            label: Context label (e.g. 'fit' or 'ema-update')
        """
        if self.feature_mean is None or self.feature_std is None or len(self.feature_mean) != TOTAL_FEATURES:
            return

        groups = [
            ("load", 0, LOOKBACK_STEPS),
            ("pv", LOOKBACK_STEPS, 2 * LOOKBACK_STEPS),
            ("temp", 2 * LOOKBACK_STEPS, 3 * LOOKBACK_STEPS),
            ("import_rate", 3 * LOOKBACK_STEPS, 4 * LOOKBACK_STEPS),
            ("export_rate", 4 * LOOKBACK_STEPS, 5 * LOOKBACK_STEPS),
            ("time", 5 * LOOKBACK_STEPS, TOTAL_FEATURES),
        ]

        parts = []
        for name, start, end in groups:
            grp_mean = float(np.mean(self.feature_mean[start:end]))
            grp_std = float(np.mean(self.feature_std[start:end]))
            parts.append("{}(mean={:.4f} std={:.4f})".format(name, grp_mean, grp_std))

        self.log("ML Predictor: Normalization stats [{}] target(mean={:.4f} std={:.4f}) {}".format(label, self.target_mean if self.target_mean is not None else 0, self.target_std if self.target_std is not None else 0, " ".join(parts)))

    def _normalize_features(self, X, fit=False, ema_alpha=0.0):
        """
        Normalize features using z-score normalization with feature-specific minimum stds.

        Args:
            X: Feature array
            fit: If True, compute and store normalization parameters
            ema_alpha: If > 0 and existing params exist, blend new stats with old via EMA
                       (new = alpha * new_stats + (1-alpha) * old_stats). Used during
                       fine-tuning to track feature distribution drift without sudden jumps.

        Returns:
            Normalized feature array
        """
        if fit:
            self.feature_mean = np.mean(X, axis=0)
            self.feature_std = np.std(X, axis=0)

            # Clamp std to per-feature minimums to prevent extreme normalization
            self.feature_std = np.maximum(self.feature_std, self._get_min_std_array(len(self.feature_std)))
            self._log_normalization_stats(label="fit")

        elif ema_alpha > 0 and self.feature_mean is not None and self.feature_std is not None:
            # EMA update: blend new statistics with existing to track distribution drift
            new_mean = np.mean(X, axis=0)
            new_std = np.std(X, axis=0)

            # Apply same min-std clamping to new stats before blending
            new_std = np.maximum(new_std, self._get_min_std_array(len(new_std)))

            # Blend: small alpha = slow drift tracking, large alpha = fast adaptation
            self.feature_mean = ema_alpha * new_mean + (1 - ema_alpha) * self.feature_mean
            self.feature_std = ema_alpha * new_std + (1 - ema_alpha) * self.feature_std
            self._log_normalization_stats(label="ema-update alpha={}".format(ema_alpha))

        if self.feature_mean is None or self.feature_std is None:
            return X

        return (X - self.feature_mean) / self.feature_std

    def _normalize_targets(self, y, fit=False):
        """
        Normalize targets using z-score normalization.

        Args:
            y: Target array
            fit: If True, compute and store normalization parameters

        Returns:
            Normalized target array
        """
        if fit:
            self.target_mean = np.mean(y)
            self.target_std = np.std(y)
            self.target_std = max(self.target_std, 1e-8)

        if self.target_mean is None or self.target_std is None:
            return y

        return (y - self.target_mean) / self.target_std

    def _denormalize_predictions(self, y_pred):
        """
        Denormalize predictions back to original scale.

        Args:
            y_pred: Normalized predictions

        Returns:
            Denormalized predictions in kWh
        """
        if self.target_mean is None or self.target_std is None:
            return y_pred

        return y_pred * self.target_std + self.target_mean

    def _clip_predictions(self, predictions, lookback_buffer=None):
        """
        Apply physical constraints to predictions.

        Args:
            predictions: Raw predictions in kWh per 5 min
            lookback_buffer: Optional recent values to compute minimum floor

        Returns:
            Clipped predictions
        """
        # Convert max kW to kWh per 5 minutes
        max_kwh_per_step = self.max_load_kw * STEP_MINUTES / 60.0

        # Compute minimum floor based on recent data (prevent collapse to zero)
        # Use 10% of the recent minimum as a floor, but at least 0.01 kWh (120W average)
        if lookback_buffer is not None and len(lookback_buffer) > 0:
            recent_min = min(lookback_buffer)
            recent_mean = sum(lookback_buffer) / len(lookback_buffer)
            # Floor is the smaller of: 20% of recent mean, or recent minimum
            min_floor = max(0.01, min(recent_min, recent_mean * 0.2))
        else:
            min_floor = 0.01  # ~120W baseline

        # Clip to valid range with minimum floor
        predictions = np.clip(predictions, min_floor, max_kwh_per_step)

        return predictions

    def train(self, load_minutes, now_utc, pv_minutes=None, temp_minutes=None, import_rates=None, export_rates=None, is_initial=True, epochs=100, time_decay_days=7, patience=5, validation_holdout_hours=24, norm_ema_alpha=0.1):
        """
        Train or fine-tune the model.

        Training uses all available data (most recent to as far back as data goes).
        Validation uses the most recent 24 hours (subset of training data) to check model fit.

        Args:
            load_minutes: Dict of {minute: cumulative_kwh}
            now_utc: Current UTC timestamp
            pv_minutes: Dict of {minute: cumulative_kwh} PV generation (backwards for history, negative for future)
            temp_minutes: Dict of {minute: temperature_celsius} Temperature (backwards for history, negative for future)
            import_rates: Dict of {minute: rate_per_kwh} Import rates (backwards for history, negative for future)
            export_rates: Dict of {minute: rate_per_kwh} Export rates (backwards for history, negative for future)
            is_initial: If True, full training; else fine-tuning on last 24h
            epochs: Number of training epochs
            time_decay_days: Time constant for sample weighting
            patience: Early stopping patience
            validation_holdout_hours: Hours of most recent data to hold out for validation
            norm_ema_alpha: EMA alpha for normalization drift tracking during fine-tuning (0=frozen, 0.1=slow drift)

        Returns:
            Validation MAE or None if training failed
        """
        self.log("ML Predictor: Starting {} training with {} epochs".format("initial" if is_initial else "fine-tune", epochs))

        # Create dataset with train/validation split
        result = self._create_dataset(
            load_minutes, now_utc, pv_minutes=pv_minutes, temp_minutes=temp_minutes, import_rates=import_rates, export_rates=export_rates, is_finetune=not is_initial, time_decay_days=time_decay_days, validation_holdout_hours=validation_holdout_hours
        )

        if result[0] is None:
            self.log("Warn: ML Predictor: Failed to create dataset")
            return None

        X_train, y_train, train_weights, X_val, y_val = result

        if len(X_train) < BATCH_SIZE:
            self.log("Warn: ML Predictor: Insufficient training data ({} samples)".format(len(X_train)))
            return None

        self.log("ML Predictor: Created {} training samples, {} validation samples".format(len(X_train), len(X_val) if X_val is not None else 0))

        # Check we have validation data
        if X_val is None or len(X_val) == 0:
            self.log("Warn: ML Predictor: No validation data available")
            return None

        # Normalize features and targets
        # On initial train: fit normalization from scratch
        # On fine-tune: apply EMA update to track distribution drift gradually
        if is_initial or not self.model_initialized:
            X_train_norm = self._normalize_features(X_train, fit=True)
            y_train_norm = self._normalize_targets(y_train, fit=True)
        else:
            X_train_norm = self._normalize_features(X_train, fit=False, ema_alpha=norm_ema_alpha)
            y_train_norm = self._normalize_targets(y_train, fit=False)
            self.log("ML Predictor: Applied EMA normalization update (alpha={}) to track feature drift".format(norm_ema_alpha))
        X_val_norm = self._normalize_features(X_val, fit=False)
        y_val_norm = self._normalize_targets(y_val, fit=False)

        # Initialize weights if needed
        if not self.model_initialized or (is_initial and self.weights is None):
            self._initialize_weights()

        # Reset Adam optimizer state for fine-tuning to prevent accumulated
        # momentum from causing overfitting on small fine-tuning datasets
        if not is_initial and self.model_initialized:
            self._reset_adam_optimizer()

        # Compute baseline validation before training (shows loaded model performance)
        baseline_mae = None
        if not is_initial:
            baseline_pred, _, _ = self._forward(X_val_norm)
            baseline_pred_denorm = self._denormalize_predictions(baseline_pred)
            baseline_mae = np.mean(np.abs(y_val - baseline_pred_denorm))
            self.log("ML Predictor: Baseline (pre-finetune) val_mae={:.4f} kWh".format(baseline_mae))

        # Training loop - use baseline as initial best for fine-tuning
        best_val_loss = baseline_mae if baseline_mae is not None else float("inf")
        patience_counter = 0
        best_weights = None
        best_biases = None

        for epoch in range(epochs):
            # Shuffle training data
            indices = np.random.permutation(len(X_train_norm))
            X_shuffled = X_train_norm[indices]
            y_shuffled = y_train_norm[indices]
            weights_shuffled = train_weights[indices]

            # Mini-batch training
            epoch_loss = 0
            num_batches = 0

            for batch_start in range(0, len(X_shuffled), BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, len(X_shuffled))
                X_batch = X_shuffled[batch_start:batch_end]
                y_batch = y_shuffled[batch_start:batch_end]
                batch_weights = weights_shuffled[batch_start:batch_end]

                # Forward pass
                y_pred, activations, pre_activations = self._forward(X_batch)

                # Compute unweighted loss for monitoring
                batch_loss = mse_loss(y_batch, y_pred)
                epoch_loss += batch_loss
                num_batches += 1

                # Backward pass with sample weights applied to gradient
                weight_grads, bias_grads = self._backward(y_batch, activations, pre_activations, sample_weights=batch_weights)

                # Adam update
                self._adam_update(weight_grads, bias_grads)

            epoch_loss /= num_batches

            # Validation
            val_pred, _, _ = self._forward(X_val_norm)
            val_pred_denorm = self._denormalize_predictions(val_pred)
            val_mae = np.mean(np.abs(y_val - val_pred_denorm))

            self.log("ML Predictor: Epoch {}/{}: train_loss={:.4f} val_mae={:.4f} kWh".format(epoch + 1, epochs, epoch_loss, val_mae))

            # Early stopping check with weight checkpointing
            if val_mae < best_val_loss:
                best_val_loss = val_mae
                patience_counter = 0
                # Checkpoint best weights
                best_weights = [w.copy() for w in self.weights]
                best_biases = [b.copy() for b in self.biases]
            else:
                patience_counter += 1

            if patience_counter >= patience:
                self.log("ML Predictor: Early stopping at epoch {}".format(epoch + 1))
                break

        # Restore best weights after early stopping
        if best_weights is not None and best_biases is not None:
            self.weights = best_weights
            self.biases = best_biases
            self.log("ML Predictor: Restored best weights from epoch with val_mae={:.4f} kWh".format(best_val_loss))

        self.training_timestamp = datetime.now(timezone.utc)
        self.validation_mae = best_val_loss
        self.epochs_trained += epochs

        self.log("ML Predictor: Training complete, final val_mae={:.4f} kWh".format(best_val_loss))

        return best_val_loss

    def predict(self, load_minutes, now_utc, midnight_utc, pv_minutes=None, temp_minutes=None, import_rates=None, export_rates=None, exog_features=None):
        """
        Generate predictions for the next 48 hours using autoregressive approach.

        Each iteration predicts the next 5-minute step, then feeds that prediction
        back into the lookback window for the next iteration. This allows the model
        to use target-time features for each prediction.

        To prevent autoregressive drift, predictions are blended with historical
        daily patterns (average energy by time of day).

        Args:
            load_minutes: Dict of {minute: cumulative_kwh}
            now_utc: Current UTC timestamp
            midnight_utc: Today's midnight UTC timestamp
            pv_minutes: Dict of {minute: cumulative_kwh} PV generation (backwards for history, negative for future)
            temp_minutes: Dict of {minute: temperature_celsius} Temperature (backwards for history, negative for future)
            import_rates: Dict of {minute: rate_per_kwh} Import rates (backwards for history, negative for future)
            export_rates: Dict of {minute: rate_per_kwh} Export rates (backwards for history, negative for future)
            exog_features: Optional dict with future exogenous data

        Returns:
            Dict of {minute: cumulative_kwh} in incrementing format for future, or empty dict on failure
        """
        if not self.model_initialized or self.weights is None:
            self.log("Warn: ML Predictor: Model not trained, cannot predict")
            return {}

        # Convert to energy per step for extracting lookback
        energy_per_step = self._load_to_energy_per_step(load_minutes)
        pv_energy_per_step = self._load_to_energy_per_step(pv_minutes) if pv_minutes else {}
        # Temperature is not cumulative, so just use the raw values
        temp_values = temp_minutes if temp_minutes else {}
        # Import and export rates are not cumulative, use raw values
        import_rate_values = import_rates if import_rates else {}
        export_rate_values = export_rates if export_rates else {}

        if not energy_per_step:
            self.log("Warn: ML Predictor: No load data available for prediction")
            return {}

        # Compute historical daily patterns for blending (prevents autoregressive drift)
        # Group historical energy by minute-of-day and compute average
        historical_pattern = self._compute_daily_pattern(energy_per_step)

        # Build initial lookback window from historical data (most recent 24 hours)
        # This will be updated as we make predictions (autoregressive)
        lookback_buffer = []
        pv_lookback_buffer = []
        temp_lookback_buffer = []
        import_rate_buffer = []
        export_rate_buffer = []
        for lb_offset in range(LOOKBACK_STEPS):
            lb_minute = lb_offset * STEP_MINUTES
            if lb_minute in energy_per_step:
                lookback_buffer.append(energy_per_step[lb_minute])
            else:
                lookback_buffer.append(0)  # Fallback to zero
            # Add PV generation (0 if no data)
            pv_lookback_buffer.append(pv_energy_per_step.get(lb_minute, 0.0))
            # Add temperature (0 if no data)
            temp_lookback_buffer.append(temp_values.get(lb_minute, 0.0))
            # Add import/export rates (0 if no data)
            import_rate_buffer.append(import_rate_values.get(lb_minute, 0.0))
            export_rate_buffer.append(export_rate_values.get(lb_minute, 0.0))

        # Autoregressive prediction loop: predict one step at a time
        predictions_energy = []

        # Blending parameters: model weight decreases as we go further into future
        # At step 0: 100% model, at step PREDICT_HORIZON: blend_floor% model
        blend_floor = 0.5  # Minimum model weight at horizon (keep more model influence)

        for step_idx in range(PREDICT_HORIZON):
            # Calculate target time for this prediction step
            target_time = now_utc + timedelta(minutes=(step_idx + 1) * STEP_MINUTES)
            minute_of_day = target_time.hour * 60 + target_time.minute
            day_of_week = target_time.weekday()
            time_features = self._create_time_features(minute_of_day, day_of_week)

            # Get PV value for the next step from forecast (negative minutes are future)
            # For future predictions, use forecast; for past, it's already in pv_energy_per_step
            future_minute = -(step_idx + 1) * STEP_MINUTES  # Negative = future
            next_pv_value = pv_energy_per_step.get(future_minute, 0.0)
            # Get temperature value for the next step from forecast (negative minutes are future)
            next_temp_value = temp_values.get(future_minute, 0.0)
            # Get import/export rate values for the next step from forecast
            next_import_rate = import_rate_values.get(future_minute, 0.0)
            next_export_rate = export_rate_values.get(future_minute, 0.0)

            # Combine features: [load_lookback..., pv_lookback..., temp_lookback..., import_rates..., export_rates..., time_features...]
            features = np.concatenate(
                [
                    np.array(lookback_buffer, dtype=np.float32),
                    np.array(pv_lookback_buffer, dtype=np.float32),
                    np.array(temp_lookback_buffer, dtype=np.float32),
                    np.array(import_rate_buffer, dtype=np.float32),
                    np.array(export_rate_buffer, dtype=np.float32),
                    time_features,
                ]
            )
            features = self._add_exog_features(features, exog_features)

            # Normalize and forward pass
            features_norm = self._normalize_features(features.reshape(1, -1), fit=False)
            pred_norm, _, _ = self._forward(features_norm)
            pred_energy = self._denormalize_predictions(pred_norm[0])

            # Apply physical constraints
            pred_energy = self._clip_predictions(pred_energy)
            model_pred = float(pred_energy[0])  # Single output

            # Get historical pattern value for this time of day
            slot = (minute_of_day // STEP_MINUTES) * STEP_MINUTES
            hist_value = historical_pattern.get(slot, model_pred)

            # Blend model prediction with historical pattern
            # Linear decay: model weight goes from 1.0 to blend_floor over horizon
            progress = step_idx / PREDICT_HORIZON
            model_weight = 1.0 - progress * (1.0 - blend_floor)
            energy_value = model_weight * model_pred + (1.0 - model_weight) * hist_value

            # Re-apply constraints after blending
            max_kwh_per_step = self.max_load_kw * STEP_MINUTES / 60.0
            energy_value = max(0.01, min(energy_value, max_kwh_per_step))

            predictions_energy.append(energy_value)

            # Update lookback buffer for next iteration (shift and add new prediction)
            # Lookback[0] is most recent, so insert at front and remove from end
            lookback_buffer.insert(0, energy_value)
            lookback_buffer.pop()  # Remove oldest value

            # Update PV lookback buffer with next forecast value
            pv_lookback_buffer.insert(0, next_pv_value)
            pv_lookback_buffer.pop()  # Remove oldest value

            # Update temperature lookback buffer with next forecast value
            temp_lookback_buffer.insert(0, next_temp_value)
            temp_lookback_buffer.pop()  # Remove oldest value

            # Update import/export rate buffers with next forecast values
            import_rate_buffer.insert(0, next_import_rate)
            import_rate_buffer.pop()  # Remove oldest value
            export_rate_buffer.insert(0, next_export_rate)
            export_rate_buffer.pop()  # Remove oldest value

        # Convert to cumulative kWh format (incrementing into future)
        # Format matches fetch_extra_load_forecast output
        result = {}
        cumulative = 0

        for step_idx in range(PREDICT_HORIZON):
            minute = step_idx * STEP_MINUTES
            energy = predictions_energy[step_idx]
            cumulative += energy
            result[minute] = round(cumulative, 4)

        return result

    def save(self, filepath):
        """
        Save model to file.

        Args:
            filepath: Path to save model (without extension)
        """
        if not self.model_initialized:
            self.log("Warn: ML Predictor: No model to save")
            return False

        try:
            # Prepare metadata
            metadata = {
                "model_version": MODEL_VERSION,
                "lookback_steps": LOOKBACK_STEPS,
                "output_steps": OUTPUT_STEPS,
                "predict_horizon": PREDICT_HORIZON,
                "hidden_sizes": HIDDEN_SIZES,
                "training_timestamp": self.training_timestamp.isoformat() if self.training_timestamp else None,
                "validation_mae": float(self.validation_mae) if self.validation_mae else None,
                "epochs_trained": self.epochs_trained,
                "learning_rate": self.learning_rate,
                "max_load_kw": self.max_load_kw,
                "feature_mean": self.feature_mean.tolist() if self.feature_mean is not None else None,
                "feature_std": self.feature_std.tolist() if self.feature_std is not None else None,
                "target_mean": float(self.target_mean) if self.target_mean is not None else None,
                "target_std": float(self.target_std) if self.target_std is not None else None,
                "pv_mean": float(self.pv_mean) if self.pv_mean is not None else None,
                "pv_std": float(self.pv_std) if self.pv_std is not None else None,
            }

            # Save weights and metadata
            save_dict = {
                "metadata_json": json.dumps(metadata),
            }

            for i, (w, b) in enumerate(zip(self.weights, self.biases)):
                save_dict[f"weight_{i}"] = w
                save_dict[f"bias_{i}"] = b

            # Save Adam optimizer state
            for i in range(len(self.weights)):
                save_dict[f"m_weight_{i}"] = self.m_weights[i]
                save_dict[f"v_weight_{i}"] = self.v_weights[i]
                save_dict[f"m_bias_{i}"] = self.m_biases[i]
                save_dict[f"v_bias_{i}"] = self.v_biases[i]

            save_dict["adam_t"] = np.array([self.adam_t])

            np.savez(filepath, **save_dict)
            self.log("ML Predictor: Model saved to {}".format(filepath))
            return True

        except Exception as e:
            self.log("Error: ML Predictor: Failed to save model: {}".format(e))
            return False

    def load(self, filepath):
        """
        Load model from file.

        Args:
            filepath: Path to model file

        Returns:
            True if successful, False otherwise
        """
        try:
            if not os.path.exists(filepath):
                self.log("ML Predictor: No saved model found at {}".format(filepath))
                return False

            data = np.load(filepath, allow_pickle=True)

            # Load metadata
            metadata = json.loads(str(data["metadata_json"]))

            # Check version compatibility
            saved_version = metadata.get("model_version", 0)
            if saved_version != MODEL_VERSION:
                self.log("Warn: ML Predictor: Model version mismatch (saved={}, current={}), retraining from scratch".format(saved_version, MODEL_VERSION))
                return False

            # Check architecture compatibility
            if metadata.get("lookback_steps") != LOOKBACK_STEPS or metadata.get("output_steps") != OUTPUT_STEPS or metadata.get("hidden_sizes") != HIDDEN_SIZES:
                self.log("Warn: ML Predictor: Architecture mismatch, retraining from scratch")
                return False

            # Load weights
            self.weights = []
            self.biases = []
            self.m_weights = []
            self.v_weights = []
            self.m_biases = []
            self.v_biases = []

            layer_count = len(HIDDEN_SIZES) + 1
            for i in range(layer_count):
                self.weights.append(data[f"weight_{i}"])
                self.biases.append(data[f"bias_{i}"])
                self.m_weights.append(data[f"m_weight_{i}"])
                self.v_weights.append(data[f"v_weight_{i}"])
                self.m_biases.append(data[f"m_bias_{i}"])
                self.v_biases.append(data[f"v_bias_{i}"])

            self.adam_t = int(data["adam_t"][0])

            # Load normalization parameters
            if metadata.get("feature_mean"):
                self.feature_mean = np.array(metadata["feature_mean"], dtype=np.float32)
            if metadata.get("feature_std"):
                self.feature_std = np.array(metadata["feature_std"], dtype=np.float32)
            if metadata.get("target_mean") is not None:
                self.target_mean = metadata["target_mean"]
            if metadata.get("target_std") is not None:
                self.target_std = metadata["target_std"]
            if metadata.get("pv_mean") is not None:
                self.pv_mean = metadata["pv_mean"]
            if metadata.get("pv_std") is not None:
                self.pv_std = metadata["pv_std"]

            # Upgrade old models: ensure minimum std thresholds to prevent extreme normalization
            if self.feature_std is not None:
                n_features = len(self.feature_std)
                old_std = self.feature_std.copy()
                self.feature_std = np.maximum(self.feature_std, self._get_min_std_array(n_features))

                if not np.array_equal(old_std, self.feature_std):
                    upgraded_count = np.sum(old_std != self.feature_std)
                    self.log("ML Predictor: Upgraded {} feature std values to prevent extreme normalization".format(upgraded_count))

            # Load training metadata
            if metadata.get("training_timestamp"):
                self.training_timestamp = datetime.fromisoformat(metadata["training_timestamp"])
            self.validation_mae = metadata.get("validation_mae")
            self.epochs_trained = metadata.get("epochs_trained", 0)

            self.model_initialized = True

            self.log("ML Predictor: Model loaded from {} (trained {}, val_mae={:.4f})".format(filepath, self.training_timestamp.strftime("%Y-%m-%d %H:%M") if self.training_timestamp else "unknown", self.validation_mae if self.validation_mae else 0))
            return True

        except Exception as e:
            self.log("Error: ML Predictor: Failed to load model: {}".format(e))
            return False

    def get_model_age_hours(self):
        """Get the age of the model in hours since last training."""
        if self.training_timestamp is None:
            return None

        age = datetime.now(timezone.utc) - self.training_timestamp
        return age.total_seconds() / 3600

    def is_valid(self, validation_threshold=2.0, max_age_hours=48):
        """
        Check if model is valid for predictions.

        Args:
            validation_threshold: Maximum acceptable validation MAE in kWh
            max_age_hours: Maximum model age in hours

        Returns:
            Tuple of (is_valid, reason_if_invalid)
        """
        if not self.model_initialized:
            return False, "not_initialized"

        if self.weights is None:
            return False, "no_weights"

        if self.validation_mae is not None and self.validation_mae > validation_threshold:
            return False, "validation_threshold"

        age_hours = self.get_model_age_hours()
        if age_hours is not None and age_hours > max_age_hours:
            return False, "stale"

        return True, None
