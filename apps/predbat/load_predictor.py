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

Implements a 4-layer feed-forward network (96-48-32-16) trained via
Adam optimiser with autoregressive prediction for 48-hour load forecasts.
Operates at CHUNK_MINUTES resolution (aggregated from 5-min raw data)
to reduce noise. Uses historical load, PV generation, temperature, and
energy rates as input features with cyclical time encoding.
"""

import numpy as np
import json
import os
from datetime import datetime, timezone, timedelta

# Architecture constants (not user-configurable)
MODEL_VERSION = 9  # Bumped to V9 for new longer history and improved architecture
STEP_MINUTES = 5  # Base data resolution in minutes (raw sensor granularity)
CHUNK_MINUTES = 5  # Prediction resolution: must be a multiple of STEP_MINUTES
CHUNK_STEPS = CHUNK_MINUTES // STEP_MINUTES  # 5-min steps aggregated per chunk (3)
LOOKBACK_STEPS = 24 * (60 // CHUNK_MINUTES)  # 24 hours at CHUNK_MINUTES resolution
OUTPUT_STEPS = 1  # Single step output (autoregressive)
PREDICT_HORIZON = 48 * (60 // CHUNK_MINUTES)  # 48 hours of predictions (96 * 30 min)
HIDDEN_SIZES = [512, 256, 128, 64]  # Deeper network with more capacity
BATCH_SIZE = 128  # Batch size

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

    def _chunk_energy_to_aligned(self, energy_per_step, now_utc):
        """
        Aggregate 5-min historical energy into aligned CHUNK_MINUTES chunks.

        Drops the leading partial chunk so that chunk boundaries align to
        CHUNK_MINUTES clock intervals (e.g. :00 and :30 for 30-min chunks).

        Args:
            energy_per_step: Dict of {minute: energy_kwh} at STEP_MINUTES resolution
                             (positive keys = historical data going backwards in time)
            now_utc: Current UTC time used to compute alignment offset

        Returns:
            Tuple of (chunked_dict, alignment_offset_minutes)
            chunked_dict: {chunk_idx: total_energy_kwh} where chunk_idx=0 is the
                          most recent complete aligned chunk
            alignment_offset_minutes: STEP_MINUTES-multiple dropped from front to align
        """
        # How many 5-min steps into the current CHUNK_MINUTES window are we?
        partial_steps = (now_utc.minute % CHUNK_MINUTES) // STEP_MINUTES
        alignment_offset = partial_steps * STEP_MINUTES

        historical_keys = [k for k in energy_per_step if k >= 0]
        if not historical_keys:
            return {}, alignment_offset

        max_minute = max(historical_keys)
        chunked = {}
        chunk_idx = 0
        minute = alignment_offset
        while minute + CHUNK_MINUTES <= max_minute + STEP_MINUTES:
            total = 0.0
            valid = True
            for j in range(CHUNK_STEPS):
                m = minute + j * STEP_MINUTES
                if m in energy_per_step:
                    total += energy_per_step[m]
                else:
                    valid = False
                    break
            if valid:
                chunked[chunk_idx] = total
            chunk_idx += 1
            minute += CHUNK_MINUTES

        return chunked, alignment_offset

    def _chunk_instantaneous_to_aligned(self, values_per_step, alignment_offset):
        """
        Aggregate instantaneous 5-min values into aligned CHUNK_MINUTES chunks by averaging.

        Used for non-cumulative features such as temperature and energy rates.

        Args:
            values_per_step: Dict of {minute: value} at STEP_MINUTES resolution
                             (positive keys = historical data)
            alignment_offset: Minutes to skip from minute-0 to align chunks
                              (obtained from _chunk_energy_to_aligned)

        Returns:
            Dict of {chunk_idx: avg_value}
        """
        historical_keys = [k for k in values_per_step if k >= 0]
        if not historical_keys:
            return {}

        max_minute = max(historical_keys)
        chunked = {}
        chunk_idx = 0
        minute = alignment_offset
        while minute + CHUNK_MINUTES <= max_minute + STEP_MINUTES:
            vals = []
            for j in range(CHUNK_STEPS):
                m = minute + j * STEP_MINUTES
                if m in values_per_step:
                    vals.append(values_per_step[m])
            if vals:
                chunked[chunk_idx] = float(np.mean(vals))
            chunk_idx += 1
            minute += CHUNK_MINUTES

        return chunked

    def _compute_daily_pattern(self, energy_per_step, smoothing_window=3):
        """
        Compute average daily pattern from historical data at CHUNK_MINUTES resolution.

        Groups 5-min energy values by CHUNK_MINUTES slot of day and averages across
        days. The result is scaled to represent total energy per CHUNK_MINUTES slot.
        Used to blend with predictions to prevent autoregressive drift.

        Args:
            energy_per_step: Dict of {minute: energy_kwh} at STEP_MINUTES resolution
            smoothing_window: Number of adjacent slots to smooth over

        Returns:
            Dict of {minute_of_day_slot: avg_energy_per_chunk} keyed at CHUNK_MINUTES intervals
        """
        # Accumulate 5-min energy values into CHUNK_MINUTES slots of the day
        by_slot = {}
        for minute, energy in energy_per_step.items():
            if minute < 0:
                continue  # Skip future data
            minute_of_day = minute % (24 * 60)  # 0-1439
            slot = (minute_of_day // CHUNK_MINUTES) * CHUNK_MINUTES
            if slot not in by_slot:
                by_slot[slot] = []
            by_slot[slot].append(energy)

        # Mean 5-min value per slot * CHUNK_STEPS = expected chunk energy
        default_energy = 0.01 * CHUNK_STEPS  # Fallback: ~120W average for one chunk
        pattern = {}
        slots_in_day = 24 * 60 // CHUNK_MINUTES
        for slot_idx in range(slots_in_day):
            slot = slot_idx * CHUNK_MINUTES
            if slot in by_slot and len(by_slot[slot]) > 0:
                pattern[slot] = float(np.mean(by_slot[slot])) * CHUNK_STEPS
            else:
                pattern[slot] = default_energy

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
        Create training dataset from load_minutes dict at CHUNK_MINUTES resolution.

        Raw 5-min data is first aggregated into CHUNK_MINUTES aligned chunks.
        Each sample uses LOOKBACK_STEPS chunks (24 h) of history to predict
        the next single chunk. Time features represent the TARGET chunk time.

        Training uses all available data; validation uses the most recent
        validation_holdout_hours as a subset to check model fit.

        Args:
            load_minutes: Dict of {minute: cumulative_kwh} going backwards in time
            now_utc: Current UTC timestamp (used for chunk alignment)
            pv_minutes: Dict of {minute: cumulative_kwh} PV generation
            temp_minutes: Dict of {minute: temperature_celsius}
            import_rates: Dict of {minute: rate_per_kwh}
            export_rates: Dict of {minute: rate_per_kwh}
            is_finetune: If True, only use last 24 hours; else use full data with time-decay
            time_decay_days: Time constant for exponential decay weighting
            validation_holdout_hours: Hours of most recent data to hold out for validation

        Returns:
            X_train, y_train, train_weights, X_val, y_val
        """
        # Convert raw cumulative data to 5-min per-step energy
        energy_per_step = self._load_to_energy_per_step(load_minutes)
        pv_energy_per_step = self._load_to_energy_per_step(pv_minutes) if pv_minutes else {}
        temp_values = temp_minutes if temp_minutes else {}
        import_rate_values = import_rates if import_rates else {}
        export_rate_values = export_rates if export_rates else {}

        if not energy_per_step:
            return None, None, None, None, None

        # Aggregate to CHUNK_MINUTES aligned chunks
        chunked_energy, alignment_offset = self._chunk_energy_to_aligned(energy_per_step, now_utc)
        chunked_pv = self._chunk_energy_to_aligned(pv_energy_per_step, now_utc)[0] if pv_energy_per_step else {}
        chunked_temp = self._chunk_instantaneous_to_aligned(temp_values, alignment_offset)
        chunked_import = self._chunk_instantaneous_to_aligned(import_rate_values, alignment_offset)
        chunked_export = self._chunk_instantaneous_to_aligned(export_rate_values, alignment_offset)

        if not chunked_energy:
            return None, None, None, None, None

        max_chunk_idx = max(chunked_energy.keys())

        # Minimum chunks required: lookback + validation window + 1 target
        min_required_chunks = LOOKBACK_STEPS + (validation_holdout_hours * 60 // CHUNK_MINUTES) + 1

        if max_chunk_idx < min_required_chunks:
            self.log("Warn: Insufficient data for ML training, need {} chunks ({} min), have {} chunks ({} min)".format(min_required_chunks, min_required_chunks * CHUNK_MINUTES, max_chunk_idx, max_chunk_idx * CHUNK_MINUTES))
            return None, None, None, None, None

        self.log("ML Predictor: Creating dataset with {} hours of training data, {} hours validation".format(max_chunk_idx * CHUNK_MINUTES // 60, validation_holdout_hours))

        # Validation window: most recent chunks (chunk_idx 0 … validation_end_chunk-1)
        validation_end_chunk = validation_holdout_hours * 60 // CHUNK_MINUTES

        X_train_list = []
        y_train_list = []
        weight_list = []
        X_val_list = []
        y_val_list = []

        def _build_sample(target_chunk_idx):
            """Build one (features, target) sample centred on target_chunk_idx."""
            lookback_start = target_chunk_idx + 1
            lookback_values = []
            pv_lookback_values = []
            temp_lookback_values = []
            import_rate_lookback = []
            export_rate_lookback = []

            for lb_offset in range(LOOKBACK_STEPS):
                lb_idx = lookback_start + lb_offset
                if lb_idx in chunked_energy:
                    lookback_values.append(chunked_energy[lb_idx])
                    pv_lookback_values.append(chunked_pv.get(lb_idx, 0.0))
                    temp_lookback_values.append(chunked_temp.get(lb_idx, 0.0))
                    import_rate_lookback.append(chunked_import.get(lb_idx, 0.0))
                    export_rate_lookback.append(chunked_export.get(lb_idx, 0.0))
                else:
                    return None, None  # Gap in data - skip

            if len(lookback_values) != LOOKBACK_STEPS:
                return None, None
            if target_chunk_idx not in chunked_energy:
                return None, None

            target_value = chunked_energy[target_chunk_idx]

            # Time features for the TARGET chunk midpoint
            target_time = now_utc - timedelta(minutes=alignment_offset + target_chunk_idx * CHUNK_MINUTES)
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
            return features, np.array([target_value], dtype=np.float32)

        # Training samples: all available chunks
        for target_chunk_idx in range(0, max_chunk_idx - LOOKBACK_STEPS):
            features, target = _build_sample(target_chunk_idx)
            if features is None:
                continue

            X_train_list.append(features)
            y_train_list.append(target)

            # Time-decay weighting (older samples get lower weight)
            age_days = (alignment_offset + target_chunk_idx * CHUNK_MINUTES) / (24 * 60)
            weight_list.append(np.exp(-age_days / time_decay_days))

        # Validation samples: most recent validation_end_chunk chunks
        for target_chunk_idx in range(0, validation_end_chunk):
            features, target = _build_sample(target_chunk_idx)
            if features is None:
                continue
            X_val_list.append(features)
            y_val_list.append(target)

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
            predictions: Raw predictions in kWh per CHUNK_MINUTES
            lookback_buffer: Optional recent values to compute minimum floor

        Returns:
            Clipped predictions
        """
        # Convert max kW to kWh per CHUNK_MINUTES
        max_kwh_per_step = self.max_load_kw * CHUNK_MINUTES / 60.0

        # Minimum floor: 0.01 kWh per 5-min step * CHUNK_STEPS (scales with chunk size)
        # This corresponds to ~120W average load as a baseline
        baseline_floor = 0.01 * CHUNK_STEPS

        # Compute minimum floor based on recent data (prevent collapse to zero)
        if lookback_buffer is not None and len(lookback_buffer) > 0:
            recent_min = min(lookback_buffer)
            recent_mean = sum(lookback_buffer) / len(lookback_buffer)
            # Floor is the smaller of: 20% of recent mean, or recent minimum
            min_floor = max(baseline_floor, min(recent_min, recent_mean * 0.1))
        else:
            min_floor = baseline_floor

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

        Raw 5-min data is aggregated into CHUNK_MINUTES aligned chunks. Each
        prediction step predicts one CHUNK_MINUTES chunk, then feeds that back
        into the lookback window. This reduces noise compared to 5-min resolution.

        Predictions are blended with historical daily patterns to prevent
        autoregressive drift.

        Args:
            load_minutes: Dict of {minute: cumulative_kwh}
            now_utc: Current UTC timestamp
            midnight_utc: Today's midnight UTC timestamp
            pv_minutes: Dict of {minute: cumulative_kwh} PV generation
            temp_minutes: Dict of {minute: temperature_celsius}
            import_rates: Dict of {minute: rate_per_kwh}
            export_rates: Dict of {minute: rate_per_kwh}
            exog_features: Optional dict with future exogenous data

        Returns:
            Dict of {minute: cumulative_kwh} keyed at STEP_MINUTES (5-min) intervals.
            Each CHUNK_MINUTES prediction is linearly interpolated into CHUNK_STEPS
            equal 5-min sub-steps so the output matches the standard load_forecast format.
            Returns empty dict on failure.
        """
        if not self.model_initialized or self.weights is None:
            self.log("Warn: ML Predictor: Model not trained, cannot predict")
            return {}

        # Convert raw cumulative data to 5-min per-step energy
        energy_per_step = self._load_to_energy_per_step(load_minutes)
        pv_energy_per_step = self._load_to_energy_per_step(pv_minutes) if pv_minutes else {}
        temp_values = temp_minutes if temp_minutes else {}
        import_rate_values = import_rates if import_rates else {}
        export_rate_values = export_rates if export_rates else {}

        if not energy_per_step:
            self.log("Warn: ML Predictor: No load data available for prediction")
            return {}

        # Compute historical daily patterns at CHUNK_MINUTES resolution for blending
        historical_pattern = self._compute_daily_pattern(energy_per_step)

        # Alignment offset: drop the leading partial chunk so buffers start on a
        # CHUNK_MINUTES clock boundary
        partial_steps = (now_utc.minute % CHUNK_MINUTES) // STEP_MINUTES
        alignment_offset = partial_steps * STEP_MINUTES

        # Build initial lookback buffers at CHUNK_MINUTES resolution (LOOKBACK_STEPS chunks)
        lookback_buffer = []
        pv_lookback_buffer = []
        temp_lookback_buffer = []
        import_rate_buffer = []
        export_rate_buffer = []

        for lb_offset in range(LOOKBACK_STEPS):
            lb_start = alignment_offset + lb_offset * CHUNK_MINUTES
            # Load: sum of CHUNK_STEPS 5-min energy values
            chunk_energy = sum(energy_per_step.get(lb_start + j * STEP_MINUTES, 0) for j in range(CHUNK_STEPS))
            lookback_buffer.append(chunk_energy)
            # PV: sum of 5-min energies over the chunk
            pv_chunk = sum(pv_energy_per_step.get(lb_start + j * STEP_MINUTES, 0) for j in range(CHUNK_STEPS))
            pv_lookback_buffer.append(pv_chunk)
            # Temperature: average over the chunk
            temp_vals = [temp_values.get(lb_start + j * STEP_MINUTES, 0) for j in range(CHUNK_STEPS)]
            temp_lookback_buffer.append(float(np.mean(temp_vals)))
            # Import rate: average over the chunk
            imp_vals = [import_rate_values.get(lb_start + j * STEP_MINUTES, 0) for j in range(CHUNK_STEPS)]
            import_rate_buffer.append(float(np.mean(imp_vals)))
            # Export rate: average over the chunk
            exp_vals = [export_rate_values.get(lb_start + j * STEP_MINUTES, 0) for j in range(CHUNK_STEPS)]
            export_rate_buffer.append(float(np.mean(exp_vals)))

        # Autoregressive prediction loop: predict one CHUNK_MINUTES chunk at a time
        predictions_energy = []
        max_kwh_per_chunk = self.max_load_kw * CHUNK_MINUTES / 60.0
        baseline_floor = 0.01 * CHUNK_STEPS

        # Blending: model weight decreases linearly from 1.0 to blend_floor
        blend_floor = 0.5

        for step_idx in range(PREDICT_HORIZON):
            # Target time: start of the step_idx-th future chunk (newer/recent edge).
            # Uses step_idx (not step_idx+1) to match training, where time features
            # represent the chunk's newer boundary (now_utc - alignment_offset -
            # target_chunk_idx * CHUNK_MINUTES), keeping training and inference aligned.
            target_time = now_utc + timedelta(minutes=step_idx * CHUNK_MINUTES)
            minute_of_day = target_time.hour * 60 + target_time.minute
            day_of_week = target_time.weekday()
            time_features = self._create_time_features(minute_of_day, day_of_week)

            # Future PV energy: sum of the CHUNK_STEPS 5-min future values for this chunk
            # Future 5-min keys are negative (e.g. -5, -10, ...) going forward in time
            next_pv_value = sum(pv_energy_per_step.get(-(step_idx * CHUNK_MINUTES + (j + 1) * STEP_MINUTES), 0.0) for j in range(CHUNK_STEPS))
            # Future temperature: average of CHUNK_STEPS 5-min future values
            temp_future = [temp_values.get(-(step_idx * CHUNK_MINUTES + (j + 1) * STEP_MINUTES), 0.0) for j in range(CHUNK_STEPS)]
            next_temp_value = float(np.mean(temp_future))
            # Future import/export rates: average over the chunk
            imp_future = [import_rate_values.get(-(step_idx * CHUNK_MINUTES + (j + 1) * STEP_MINUTES), 0.0) for j in range(CHUNK_STEPS)]
            next_import_rate = float(np.mean(imp_future))
            exp_future = [export_rate_values.get(-(step_idx * CHUNK_MINUTES + (j + 1) * STEP_MINUTES), 0.0) for j in range(CHUNK_STEPS)]
            next_export_rate = float(np.mean(exp_future))

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
            model_pred = float(pred_energy[0])

            # Blend with historical daily pattern (CHUNK_MINUTES slot)
            slot = (minute_of_day // CHUNK_MINUTES) * CHUNK_MINUTES
            hist_value = historical_pattern.get(slot, model_pred)

            # Linear blend: 100% model at step 0, blend_floor% model at horizon
            progress = step_idx / PREDICT_HORIZON
            model_weight = 1.0 - progress * (1.0 - blend_floor)
            energy_value = model_weight * model_pred + (1.0 - model_weight) * hist_value

            # Re-apply hard constraints after blending
            energy_value = max(baseline_floor, min(energy_value, max_kwh_per_chunk))

            predictions_energy.append(energy_value)

            # Shift lookback buffers: insert new prediction at front, drop oldest
            lookback_buffer.insert(0, energy_value)
            lookback_buffer.pop()
            pv_lookback_buffer.insert(0, next_pv_value)
            pv_lookback_buffer.pop()
            temp_lookback_buffer.insert(0, next_temp_value)
            temp_lookback_buffer.pop()
            import_rate_buffer.insert(0, next_import_rate)
            import_rate_buffer.pop()
            export_rate_buffer.insert(0, next_export_rate)
            export_rate_buffer.pop()

        # Convert to cumulative kWh format at STEP_MINUTES resolution.
        # Each CHUNK_MINUTES prediction is split into CHUNK_STEPS equal 5-min sub-steps
        # so that the output matches the format expected by fetch_extra_load_forecast
        # (incrementing kWh, one entry per STEP_MINUTES).
        result = {}
        cumulative = 0

        for step_idx in range(PREDICT_HORIZON):
            energy = predictions_energy[step_idx]
            energy_per_substep = energy / CHUNK_STEPS
            for j in range(CHUNK_STEPS):
                minute = step_idx * CHUNK_MINUTES + j * STEP_MINUTES
                cumulative += energy_per_substep
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
