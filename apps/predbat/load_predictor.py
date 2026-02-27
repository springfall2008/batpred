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

Implements a 3-hidden-layer feed-forward network ([512, 256, 64]) trained via
AdamW optimiser with cosine LR decay and Huber loss. Uses autoregressive
prediction for 48-hour load forecasts at CHUNK_MINUTES (5-min) resolution.
Inputs: historical load, PV generation, temperature, import/export rates,
and cyclical time features (minute-of-day, day-of-week, day-of-year).
"""

import numpy as np
import json
import os
from datetime import datetime, timezone, timedelta

# Architecture constants (not user-configurable)
MODEL_VERSION = 10  # Bumped to V10 for day-of-year seasonality features and network layer changes
STEP_MINUTES = 5  # Base data resolution in minutes (raw sensor granularity)
CHUNK_MINUTES = 5  # Prediction resolution: must be a multiple of STEP_MINUTES
CHUNK_STEPS = CHUNK_MINUTES // STEP_MINUTES  # 5-min steps aggregated per chunk (3)
LOOKBACK_STEPS = 24 * (60 // CHUNK_MINUTES)  # 24 hours at CHUNK_MINUTES resolution
OUTPUT_STEPS = 1  # Single step output (autoregressive)
PREDICT_HORIZON = 48 * (60 // CHUNK_MINUTES)  # 48 hours of predictions (96 * 30 min)
HIDDEN_SIZES = [512, 256, 64]  # Deeper network with more capacity
BATCH_SIZE = 128  # Batch size
MAX_BATCHES_PER_EPOCH = 200  # Cap on SGD batches per epoch - with importance sampling this keeps training
# cost constant regardless of how many days of history are loaded

# Feature constants
NUM_TIME_FEATURES = 6  # sin/cos minute-of-day, sin/cos day-of-week, sin/cos day-of-year (for TARGET time)
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


class LoadPredictor:
    """
    Lightweight MLP-based load predictor using NumPy only.

    Predicts household electrical load for the next 48 hours using:
    - Historical load data (lookback window)
    - Cyclical time encodings (hour-of-day, day-of-week)
    - Placeholder for future exogenous features (temperature, solar)
    """

    def __init__(self, log_func=None, learning_rate=0.001, max_load_kw=23.0, weight_decay=0.01, dropout_rate=0.1):
        """
        Initialize the load predictor.

        Args:
            log_func: Logging function (defaults to print)
            learning_rate: Learning rate for Adam optimizer
            max_load_kw: Maximum load in kW for clipping predictions
            weight_decay: L2 regularization coefficient for AdamW (0.0 disables)
            dropout_rate: Fraction of hidden-layer neurons to drop during training (0.0 disables);
                          inverted dropout is used so inference requires no scaling
        """
        self.log = log_func if log_func else print
        self.learning_rate = learning_rate
        self.max_load_kw = max_load_kw
        self.weight_decay = weight_decay
        self.dropout_rate = dropout_rate

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
        self.validation_bias = None  # Signed metric: mean(predicted - actual); + = over-predicting, - = under-predicting
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

    def _forward(self, X, training=False):
        """
        Forward pass through the network.

        Args:
            X: Input features (batch_size, TOTAL_FEATURES)
            training: If True, apply inverted dropout to hidden layers

        Returns:
            Tuple of (output, activations, pre_activations, dropout_masks).
            dropout_masks is a list with one entry per hidden layer (None when
            dropout is disabled or training=False).
        """
        activations = [X]
        pre_activations = []
        dropout_masks = []

        current = X
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            z = np.dot(current, w) + b
            pre_activations.append(z)

            # Apply ReLU for hidden layers, linear for output
            if i < len(self.weights) - 1:
                current = relu(z)
                # Inverted dropout: scale kept neurons by 1/(1-p) so inference
                # runs without any scaling adjustment
                if training and self.dropout_rate > 0.0:
                    mask = (np.random.rand(*current.shape) > self.dropout_rate).astype(np.float32)
                    mask /= 1.0 - self.dropout_rate
                    current = current * mask
                    dropout_masks.append(mask)
                else:
                    dropout_masks.append(None)
            else:
                current = z  # Linear output

            activations.append(current)

        return current, activations, pre_activations, dropout_masks

    def _backward(self, y_true, activations, pre_activations, sample_weights=None, dropout_masks=None, huber_delta=1.35):
        """
        Backward pass using backpropagation.

        Args:
            y_true: True target values
            activations: Layer activations from forward pass
            pre_activations: Pre-activation values from forward pass
            sample_weights: Optional per-sample weights for weighted loss
            dropout_masks: Optional list of dropout masks from _forward (one per hidden layer);
                           None entries mean no dropout was applied to that layer
            huber_delta: Huber loss transition point in normalised target units (default 1.35;
                         errors below this are quadratic, above are linear)

        Returns:
            Gradients for weights and biases
        """
        # Output layer gradient (Huber loss derivative - robust to outlier spikes)
        delta = huber_loss_derivative(y_true, activations[-1], delta=huber_delta)

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
                # Apply dropout mask: zero out gradients for dropped neurons
                # (mask already carries the inverted scaling factor 1/(1-p))
                if dropout_masks is not None and (i - 1) < len(dropout_masks) and dropout_masks[i - 1] is not None:
                    delta = delta * dropout_masks[i - 1]

        return weight_grads, bias_grads

    def _adam_update(self, weight_grads, bias_grads, beta1=0.9, beta2=0.999, epsilon=1e-8, lr=None):
        """
        Update weights using Adam optimizer with optional weight decay (AdamW).

        Args:
            weight_grads: Gradients for weights
            bias_grads: Gradients for biases
            beta1: Exponential decay rate for first moment
            beta2: Exponential decay rate for second moment
            epsilon: Small constant for numerical stability
            lr: Learning rate override (if None, uses self.learning_rate)
        """
        self.adam_t += 1
        effective_lr = lr if lr is not None else self.learning_rate

        for i in range(len(self.weights)):
            # Update momentum for weights
            self.m_weights[i] = beta1 * self.m_weights[i] + (1 - beta1) * weight_grads[i]
            self.v_weights[i] = beta2 * self.v_weights[i] + (1 - beta2) * (weight_grads[i] ** 2)

            # Bias correction
            m_hat = self.m_weights[i] / (1 - beta1**self.adam_t)
            v_hat = self.v_weights[i] / (1 - beta2**self.adam_t)

            # Update weights with Adam step
            self.weights[i] -= effective_lr * m_hat / (np.sqrt(v_hat) + epsilon)

            # Apply weight decay (AdamW-style L2 regularization)
            if self.weight_decay > 0:
                self.weights[i] *= 1 - effective_lr * self.weight_decay

            # Update momentum for biases
            self.m_biases[i] = beta1 * self.m_biases[i] + (1 - beta1) * bias_grads[i]
            self.v_biases[i] = beta2 * self.v_biases[i] + (1 - beta2) * (bias_grads[i] ** 2)

            # Bias correction
            m_hat = self.m_biases[i] / (1 - beta1**self.adam_t)
            v_hat = self.v_biases[i] / (1 - beta2**self.adam_t)

            # Update biases (no weight decay on biases)
            self.biases[i] -= effective_lr * m_hat / (np.sqrt(v_hat) + epsilon)

    def _create_time_features(self, minute_of_day, day_of_week, day_of_year=1):
        """
        Create cyclical time features.

        Args:
            minute_of_day: Minutes since midnight (0-1439)
            day_of_week: Day of week (0-6, Monday=0)
            day_of_year: Day of year (1-365/366)

        Returns:
            Array of 6 time features: sin/cos minute, sin/cos day-of-week, sin/cos day-of-year
        """
        # Cyclical encoding for minute of day
        minute_sin = np.sin(2 * np.pi * minute_of_day / 1440)
        minute_cos = np.cos(2 * np.pi * minute_of_day / 1440)

        # Cyclical encoding for day of week
        day_sin = np.sin(2 * np.pi * day_of_week / 7)
        day_cos = np.cos(2 * np.pi * day_of_week / 7)

        # Cyclical encoding for day of year (seasonality)
        season_sin = np.sin(2 * np.pi * (day_of_year - 1) / 365)
        season_cos = np.cos(2 * np.pi * (day_of_year - 1) / 365)

        return np.array([minute_sin, minute_cos, day_sin, day_cos, season_sin, season_cos], dtype=np.float32)

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

    def _compute_daily_pattern(self, energy_per_step, now_utc, smoothing_window=3, min_obs_per_slot=2):
        """
        Compute per-day-of-week daily patterns from historical data at CHUNK_MINUTES resolution.

        Maintains 7 separate patterns (Monday=0 … Sunday=6) so weekday vs weekend
        load profiles are captured independently. Falls back to the global (all-days)
        pattern for any DOW that has fewer than min_obs_per_slot observations per slot,
        which avoids noisy estimates from sparse data.

        Args:
            energy_per_step: Dict of {minute: energy_kwh} at STEP_MINUTES resolution
                             (positive keys = minutes back from now_utc)
            now_utc: Current UTC timestamp, used to map minute offsets to real datetimes
            smoothing_window: Number of adjacent slots to smooth over
            min_obs_per_slot: Minimum observations per slot for a DOW pattern to be
                              used; DOWs below this threshold fall back to the global pattern

        Returns:
            Dict of {day_of_week: {minute_of_day_slot: avg_energy_per_chunk}}
            where day_of_week 0=Monday … 6=Sunday.
            The global fallback pattern is stored under key None.
        """
        default_energy = 0.01 * CHUNK_STEPS  # ~120W baseline per chunk
        slots_in_day = 24 * 60 // CHUNK_MINUTES

        # Accumulate per-DOW slot observations
        # by_dow_slot[dow][slot] = [energy, ...]
        by_dow_slot = {dow: {} for dow in range(7)}
        by_slot_global = {}  # Global fallback (all DOWs combined)

        for minute, energy in energy_per_step.items():
            if minute < 0:
                continue  # Skip future data
            actual_time = now_utc - timedelta(minutes=minute)
            dow = actual_time.weekday()
            minute_of_day = actual_time.hour * 60 + actual_time.minute
            slot = (minute_of_day // CHUNK_MINUTES) * CHUNK_MINUTES

            by_dow_slot[dow].setdefault(slot, []).append(energy)
            by_slot_global.setdefault(slot, []).append(energy)

        def _build_smoothed_pattern(by_slot):
            """Average, scale, and smooth a {slot: [values]} accumulator."""
            raw = {}
            for slot_idx in range(slots_in_day):
                slot = slot_idx * CHUNK_MINUTES
                vals = by_slot.get(slot, [])
                raw[slot] = float(np.mean(vals)) * CHUNK_STEPS if vals else default_energy
            slots = sorted(raw.keys())
            smoothed = {}
            for i, slot in enumerate(slots):
                window_vals = [raw[slots[(i + off) % len(slots)]] for off in range(-smoothing_window // 2, smoothing_window // 2 + 1)]
                smoothed[slot] = float(np.mean(window_vals))
            return smoothed

        global_pattern = _build_smoothed_pattern(by_slot_global)

        patterns = {None: global_pattern}  # Fallback always available
        for dow in range(7):
            slot_data = by_dow_slot[dow]
            # Use DOW-specific pattern only if every slot has enough observations
            obs_counts = [len(slot_data.get(slot_idx * CHUNK_MINUTES, [])) for slot_idx in range(slots_in_day)]
            if obs_counts and min(obs_counts) >= min_obs_per_slot:
                patterns[dow] = _build_smoothed_pattern(slot_data)
            else:
                patterns[dow] = global_pattern  # Fall back to global

        return patterns

    def _create_dataset(self, load_minutes, now_utc, pv_minutes=None, temp_minutes=None, import_rates=None, export_rates=None, is_finetune=False, time_decay_days=7, validation_holdout_hours=24):
        """
        Create training dataset from load_minutes dict at CHUNK_MINUTES resolution.

        Raw 5-min data is first aggregated into CHUNK_MINUTES aligned chunks.
        Each sample uses LOOKBACK_STEPS chunks (24 h) of history to predict
        the next single chunk. Time features represent the TARGET chunk time.

        Training uses all available data; validation uses the most recent
        validation_holdout_hours as a subset to check model fit.

        Args:
            load_minutes: Dict of {minute: kwh_per_5min} going backwards in time (pre-converted by load_ml_component)
            now_utc: Current UTC timestamp (used for chunk alignment)
            pv_minutes: Dict of {minute: kwh_per_5min} PV generation (positive=historical, negative=future)
            temp_minutes: Dict of {minute: temperature_celsius}
            import_rates: Dict of {minute: rate_per_kwh}
            export_rates: Dict of {minute: rate_per_kwh}
            is_finetune: If True, only use last 24 hours; else use full data with time-decay
            time_decay_days: Time constant for exponential decay weighting
            validation_holdout_hours: Hours of most recent data to hold out for validation

        Returns:
            X_train, y_train, train_weights, X_val, y_val
        """
        # Data is already in per-5-min energy format (pre-converted in load_ml_component)
        energy_per_step = load_minutes
        pv_energy_per_step = pv_minutes if pv_minutes else {}
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
            day_of_year = target_time.timetuple().tm_yday
            time_features = self._create_time_features(minute_of_day, day_of_week, day_of_year)

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

    def _ar_rollout_diagnostic(self, load_minutes, now_utc, pv_minutes=None, temp_minutes=None, import_rates=None, export_rates=None, validation_holdout_hours=24):
        """
        Run an autoregressive rollout over the validation holdout period.

        Unlike teacher-forced validation (which evaluates each step with real context),
        this feeds each predicted chunk back as the next step's context, exposing
        how compounding errors accumulate over the holdout window.

        Args:
            load_minutes: Dict of {minute: kwh_per_step} historical load data
            now_utc: Current UTC timestamp used during training
            pv_minutes: Dict of {minute: kwh_per_step} PV data
            temp_minutes: Dict of {minute: celsius} temperature data
            import_rates: Dict of {minute: rate} import tariff data
            export_rates: Dict of {minute: rate} export tariff data
            validation_holdout_hours: Size of the holdout window (must match training)

        Returns:
            (ar_mae, ar_bias) in kWh per chunk, or (None, None) on failure.
        """
        if not self.model_initialized or self.weights is None:
            return None, None

        energy_per_step = load_minutes
        pv_energy_per_step = pv_minutes if pv_minutes else {}
        temp_values = temp_minutes if temp_minutes else {}
        import_rate_values = import_rates if import_rates else {}
        export_rate_values = export_rates if export_rates else {}

        if not energy_per_step:
            return None, None

        # Chunk data identically to _create_dataset
        chunked_energy, alignment_offset = self._chunk_energy_to_aligned(energy_per_step, now_utc)
        chunked_pv = self._chunk_energy_to_aligned(pv_energy_per_step, now_utc)[0] if pv_energy_per_step else {}
        chunked_temp = self._chunk_instantaneous_to_aligned(temp_values, alignment_offset)
        chunked_import = self._chunk_instantaneous_to_aligned(import_rate_values, alignment_offset)
        chunked_export = self._chunk_instantaneous_to_aligned(export_rate_values, alignment_offset)

        if not chunked_energy:
            return None, None

        # Holdout window: chunks 0 .. validation_end_chunk-1 (most-recent = 0)
        validation_end_chunk = validation_holdout_hours * 60 // CHUNK_MINUTES

        # We need LOOKBACK_STEPS real chunks immediately before the holdout as context
        context_start = validation_end_chunk  # chunk just before the holdout starts
        if (context_start + LOOKBACK_STEPS - 1) not in chunked_energy:
            return None, None

        # Initialise lookback buffers from real data (index 0 = most-recent context)
        ar_load = [chunked_energy.get(context_start + lb, 0.0) for lb in range(LOOKBACK_STEPS)]
        ar_pv = [chunked_pv.get(context_start + lb, 0.0) for lb in range(LOOKBACK_STEPS)]
        ar_temp = [chunked_temp.get(context_start + lb, 0.0) for lb in range(LOOKBACK_STEPS)]
        ar_imp = [chunked_import.get(context_start + lb, 0.0) for lb in range(LOOKBACK_STEPS)]
        ar_exp = [chunked_export.get(context_start + lb, 0.0) for lb in range(LOOKBACK_STEPS)]

        errors = []
        biases = []

        # Step from oldest holdout chunk down to newest (autoregressive order)
        for step in range(validation_end_chunk):
            target_chunk_idx = validation_end_chunk - 1 - step
            if target_chunk_idx not in chunked_energy:
                continue

            true_value = chunked_energy[target_chunk_idx]

            target_time = now_utc - timedelta(minutes=alignment_offset + target_chunk_idx * CHUNK_MINUTES)
            minute_of_day = target_time.hour * 60 + target_time.minute
            day_of_week = target_time.weekday()
            day_of_year = target_time.timetuple().tm_yday
            time_features = self._create_time_features(minute_of_day, day_of_week, day_of_year)

            features = np.concatenate(
                [
                    np.array(ar_load, dtype=np.float32),
                    np.array(ar_pv, dtype=np.float32),
                    np.array(ar_temp, dtype=np.float32),
                    np.array(ar_imp, dtype=np.float32),
                    np.array(ar_exp, dtype=np.float32),
                    time_features,
                ]
            )

            features_norm = self._normalize_features(features.reshape(1, -1), fit=False)
            pred_norm, _, _, _ = self._forward(features_norm, training=False)
            pred_value = float(self._clip_predictions(self._denormalize_predictions(pred_norm[0]))[0])

            errors.append(abs(pred_value - true_value))
            biases.append(pred_value - true_value)

            # Feed predicted load back; use real exogenous values (they are known history)
            ar_load.insert(0, pred_value)
            ar_load.pop()
            ar_pv.insert(0, chunked_pv.get(target_chunk_idx, 0.0))
            ar_pv.pop()
            ar_temp.insert(0, chunked_temp.get(target_chunk_idx, 0.0))
            ar_temp.pop()
            ar_imp.insert(0, chunked_import.get(target_chunk_idx, 0.0))
            ar_imp.pop()
            ar_exp.insert(0, chunked_export.get(target_chunk_idx, 0.0))
            ar_exp.pop()

        if not errors:
            return None, None

        return float(np.mean(errors)), float(np.mean(biases))

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

    def train(
        self,
        load_minutes,
        now_utc,
        pv_minutes=None,
        temp_minutes=None,
        import_rates=None,
        export_rates=None,
        is_initial=True,
        epochs=100,
        time_decay_days=7,
        patience=5,
        validation_holdout_hours=24,
        norm_ema_alpha=0.1,
        lr_decay="cosine",
        ema_smoothing_alpha=0.3,
        huber_delta=1.35,
    ):
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
            lr_decay: Learning rate schedule - "cosine" decays from lr to 0.1*lr over all epochs, None keeps lr constant
            huber_delta: Huber loss transition point in normalised target units (default 1.35; errors within
                         this threshold are penalised quadratically, beyond it linearly)
            ema_smoothing_alpha: EMA alpha for smoothing the early-stopping metric across epochs (0=no smoothing, 0.3=moderate)

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
        baseline_bias = 0.0
        if not is_initial:
            baseline_pred, _, _, _ = self._forward(X_val_norm)
            baseline_pred_denorm = self._denormalize_predictions(baseline_pred)
            baseline_mae = np.mean(np.abs(y_val - baseline_pred_denorm))
            baseline_bias = float(np.mean(baseline_pred_denorm - y_val))
            self.log("ML Predictor: Baseline (pre-finetune) val_mae={:.4f} kWh val_bias={:+.4f} kWh".format(baseline_mae, baseline_bias))

        # Training loop - combined metric (val_mae + 0.5 * |val_bias|) used for early stopping
        # to penalise both absolute error and systematic over/under-prediction
        best_val_loss = baseline_mae if baseline_mae is not None else float("inf")
        best_val_bias = baseline_bias  # Track actual baseline bias, not 0.0
        best_combined = (0.5 * baseline_mae + abs(baseline_bias)) if baseline_mae is not None else float("inf")
        patience_counter = 0
        # Checkpoint current weights as the starting best so that if no epoch improves
        # on the baseline, we correctly restore these weights (not the final epoch's weights)
        best_weights = [w.copy() for w in self.weights]
        best_biases = [b.copy() for b in self.biases]

        # Pre-compute normalised sampling probabilities from time-decay weights.
        # Importance sampling: p(i) ∝ weight(i) so the expected gradient is already
        # correctly weighted - we do NOT additionally scale inside _backward.
        sampling_probs = train_weights / train_weights.sum()

        # Number of samples to draw per epoch is capped at MAX_BATCHES_PER_EPOCH * BATCH_SIZE.
        # This keeps per-epoch cost constant regardless of how large the history is.
        n_epoch_samples = min(MAX_BATCHES_PER_EPOCH * BATCH_SIZE, len(X_train_norm))
        if len(X_train_norm) > MAX_BATCHES_PER_EPOCH * BATCH_SIZE:
            self.log("ML Predictor: Large dataset ({} samples) - using importance sampling ({} samples/epoch, {} batches)".format(len(X_train_norm), n_epoch_samples, n_epoch_samples // BATCH_SIZE))

        # Cosine LR schedule bounds
        lr_max = self.learning_rate
        lr_min = 0.1 * self.learning_rate

        # EMA accumulator for early-stopping metric (seeds on first epoch)
        ema_combined = None

        for epoch in range(epochs):
            # Cosine LR decay: lr_t decays from lr_max at epoch 0 to lr_min at the final epoch
            if lr_decay == "cosine":
                lr_t = lr_min + 0.5 * (lr_max - lr_min) * (1.0 + np.cos(np.pi * epoch / max(epochs - 1, 1)))
            else:
                lr_t = lr_max

            # Draw samples with probability proportional to time-decay weights (with replacement).
            # Recent data is sampled more often; old data still contributes but rarely.
            sampled_indices = np.random.choice(len(X_train_norm), size=n_epoch_samples, replace=True, p=sampling_probs)
            X_epoch = X_train_norm[sampled_indices]
            y_epoch = y_train_norm[sampled_indices]

            # Mini-batch training
            epoch_loss = 0
            num_batches = 0

            for batch_start in range(0, n_epoch_samples, BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, n_epoch_samples)
                X_batch = X_epoch[batch_start:batch_end]
                y_batch = y_epoch[batch_start:batch_end]

                # Forward pass (training=True enables dropout)
                y_pred, activations, pre_activations, dropout_masks = self._forward(X_batch, training=True)

                # Compute Huber loss for monitoring (in normalised space)
                batch_loss = huber_loss(y_batch, y_pred, delta=huber_delta)
                epoch_loss += batch_loss
                num_batches += 1

                # Backward pass - no additional sample_weights needed because importance
                # is already encoded in the sampling distribution
                weight_grads, bias_grads = self._backward(y_batch, activations, pre_activations, dropout_masks=dropout_masks, huber_delta=huber_delta)

                # Adam update with current cosine LR
                self._adam_update(weight_grads, bias_grads, lr=lr_t)

            epoch_loss /= num_batches

            # Validation (training=False: no dropout for deterministic evaluation)
            val_pred, _, _, _ = self._forward(X_val_norm)
            val_pred_denorm = self._denormalize_predictions(val_pred)
            val_mae = np.mean(np.abs(y_val - val_pred_denorm))
            # Median bias for early stopping: more robust than mean when validation set
            # contains a few outlier samples (e.g. a single EV charging spike).
            val_bias_median = float(np.median(val_pred_denorm - y_val))
            # Mean bias for human-readable logging
            val_bias_mean = float(np.mean(val_pred_denorm - y_val))
            val_mean_actual = np.mean(y_val) if np.mean(y_val) > 1e-8 else 1e-8
            val_bias_pct = 100.0 * val_bias_mean / float(val_mean_actual)
            # Combined metric uses median bias so a single outlier epoch does not gate checkpointing
            val_combined = val_mae * 0.5 + abs(val_bias_median)

            # EMA-smooth the combined metric to reduce noise from stochastic sampling
            if ema_combined is None:
                ema_combined = val_combined  # Seed on first epoch
            else:
                ema_combined = ema_smoothing_alpha * val_combined + (1.0 - ema_smoothing_alpha) * ema_combined

            self.log(
                "ML Predictor: Epoch {}/{}: lr={:.5f} huber_loss={:.4f} val_mae={:.4f} kWh val_bias={:+.4f} kWh ({:+.1f}%) combined={:.4f} kWh ema_combined={:.4f} kWh".format(
                    epoch + 1, epochs, lr_t, epoch_loss, val_mae, val_bias_mean, val_bias_pct, val_combined, ema_combined
                )
            )

            # Early stopping uses EMA-smoothed combined metric so a single noisy epoch
            # cannot prematurely checkpoint a suboptimal set of weights.
            # Weight checkpointing uses the raw epoch weights (not a smoothed version).
            if ema_combined < best_combined:
                best_combined = ema_combined
                best_val_loss = val_mae
                best_val_bias = val_bias_mean
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
            self.log(
                "ML Predictor: Restored best weights from epoch with val_mae={:.4f} kWh val_bias={:+.4f} kWh ({:+.1f}%)".format(
                    best_val_loss, float(best_val_bias), 100.0 * float(best_val_bias) / (float(np.mean(y_val)) if float(np.mean(y_val)) > 1e-8 else 1e-8)
                )
            )

        self.training_timestamp = datetime.now(timezone.utc)
        self.validation_mae = best_val_loss
        self.validation_bias = float(best_val_bias)
        self.epochs_trained += epochs

        self.log("ML Predictor: Training complete, final val_mae={:.4f} kWh val_bias={:+.4f} kWh ({:+.1f}%)".format(best_val_loss, float(best_val_bias), 100.0 * float(best_val_bias) / (float(np.mean(y_val)) if float(np.mean(y_val)) > 1e-8 else 1e-8)))

        # Autoregressive diagnostic: run a full AR rollout over the holdout period
        # to expose compounding error (teacher-forced val_mae won't show this)
        ar_mae, ar_bias = self._ar_rollout_diagnostic(
            load_minutes,
            now_utc,
            pv_minutes=pv_minutes,
            temp_minutes=temp_minutes,
            import_rates=import_rates,
            export_rates=export_rates,
            validation_holdout_hours=validation_holdout_hours,
        )
        if ar_mae is not None:
            mean_y_val = float(np.mean(y_val)) if float(np.mean(y_val)) > 1e-8 else 1e-8
            ar_drift = ar_mae - best_val_loss
            self.log("ML Predictor: AR rollout over holdout: ar_mae={:.4f} kWh ar_bias={:+.4f} kWh ({:+.1f}%) [drift vs teacher-forced: {:+.4f} kWh]".format(ar_mae, ar_bias, 100.0 * ar_bias / mean_y_val, ar_drift))

        return best_val_loss

    @staticmethod
    def _slice_data_dict(data_dict, start_minute, end_minute):
        """
        Slice a {minute: value} data dict to the range [start_minute, end_minute]
        and re-index so that start_minute becomes key 0.

        This shifts the view window so that the "most recent" edge of the slice
        lands at key 0, which is what _create_dataset() expects when computing the
        validation holdout (it uses the lowest-key chunks as validation).

        IMPORTANT: Because the keys are re-indexed, the caller MUST pass an adjusted
        `now_utc` to train() / _create_dataset() to keep time features correct.
        Specifically, pass `now_utc - timedelta(minutes=start_minute)` so that
        chunk key 0 maps to the real wall-clock time of the slice's newest edge.

        Args:
            data_dict: Dict of {minute: value} with positive keys = minutes back from now
            start_minute: Inclusive lower bound (more-recent edge of the slice)
            end_minute: Inclusive upper bound (older edge of the slice)

        Returns:
            New dict with keys shifted so start_minute → 0
        """
        if not data_dict:
            return {}
        return {k - start_minute: v for k, v in data_dict.items() if start_minute <= k <= end_minute}

    def train_curriculum(
        self,
        load_minutes,
        now_utc,
        pv_minutes=None,
        temp_minutes=None,
        import_rates=None,
        export_rates=None,
        epochs=100,
        time_decay_days=30,
        patience=5,
        validation_holdout_hours=24,
        norm_ema_alpha=0.1,
        curriculum_window_days=7,
        curriculum_step_days=7,
        max_intermediate_passes=0,
        huber_delta=1.35,
    ):
        """
                Train using curriculum learning: progressively expand the training window
                from the oldest available data forward, using the following 24 hours as the
                holdout at each intermediate pass.

                Pass structure (example: 4 weeks of data, window=1, step=1):
                  Pass 1/4: slice = oldest 1 week, holdout = most-recent 24 h of that slice
                  Pass 2/4: slice = oldest 2 weeks, holdout = most-recent 24 h of that slice
                  Pass 3/4: slice = oldest 3 weeks, holdout = most-recent 24 h of that slice
                  Final 4/4: full data, holdout = most-recent validation_holdout_hours

                If there is insufficient data for even one intermediate pass (less than
        curriculum_window_days days of history), falls back to a single train() call.

                All curriculum sizing parameters are explicit keyword arguments so they can
                be driven from component config without touching this method.

                Args:
                    load_minutes: Dict of {minute: energy_kwh} (positive keys = minutes back from now)
                    now_utc: Current UTC timestamp
                    pv_minutes: Dict of {minute: energy_kwh} PV generation
                    temp_minutes: Dict of {minute: temperature_celsius}
                    import_rates: Dict of {minute: rate_per_kwh}
                    export_rates: Dict of {minute: rate_per_kwh}
                    epochs: Epochs per pass
                    time_decay_days: Time-decay constant for sample weighting
                    patience: Early-stopping patience per pass
                    validation_holdout_hours: Holdout window for the final pass
                    norm_ema_alpha: Normalisation EMA alpha for passes after the first
                    curriculum_window_days: Initial training window size in days (default 7)
                    curriculum_step_days: Days added per subsequent pass (default 7)
                    max_intermediate_passes: Maximum number of intermediate passes to run;
                                             0 (default) means no limit. When > 0, only the
                                             last N windows (largest/most-recent slices) are
                                             used, skipping the very earliest small windows.
                    huber_delta: Huber loss transition point passed through to each train() call

                Returns:
                    Validation MAE from the final pass, or None if all passes failed.
        """
        # Build list of positive minute keys to find total history span
        hist_minutes = [k for k in load_minutes if isinstance(k, int) and k > 0]
        if not hist_minutes:
            self.log("Warn: ML Predictor: Curriculum training - no historical data")
            return None

        max_minute = max(hist_minutes)
        day_minutes = 24 * 60  # 1 440 minutes per day

        # Normalise optional dicts so we can always pass them to _slice_data_dict
        pv_minutes = pv_minutes or {}
        temp_minutes = temp_minutes or {}
        import_rates = import_rates or {}
        export_rates = export_rates or {}

        # Intermediate window sizes (minutes): initial window, initial+step, ... up to but
        # not including the full dataset (the final unrestricted pass covers that).
        initial_window = curriculum_window_days * day_minutes
        step_window = curriculum_step_days * day_minutes
        window_sizes = list(range(initial_window, max_minute, step_window))

        # If a pass cap is set, keep only the last N entries (largest windows, closest to full data)
        if max_intermediate_passes > 0 and len(window_sizes) > max_intermediate_passes:
            window_sizes = window_sizes[-max_intermediate_passes:]
            self.log("ML Predictor: Curriculum training - capped to last {} intermediate passes (starting at window={:.1f} days)".format(max_intermediate_passes, window_sizes[0] / day_minutes))

        if not window_sizes:
            # Less data than the initial window — skip curriculum, single pass
            self.log("ML Predictor: Curriculum training - insufficient data ({:.1f} days) for multiple passes, using single-pass training".format(max_minute / day_minutes))
            return self.train(
                load_minutes,
                now_utc,
                pv_minutes=pv_minutes,
                temp_minutes=temp_minutes,
                import_rates=import_rates,
                export_rates=export_rates,
                is_initial=True,
                epochs=epochs,
                time_decay_days=time_decay_days,
                patience=patience,
                validation_holdout_hours=validation_holdout_hours,
                norm_ema_alpha=norm_ema_alpha,
                huber_delta=huber_delta,
            )

        total_passes = len(window_sizes) + 1  # intermediate passes + final full pass
        self.log("ML Predictor: Curriculum training - {} passes, window {:.1f}→{:.1f} days + final full pass ({:.1f} days)".format(total_passes, window_sizes[0] / day_minutes, window_sizes[-1] / day_minutes, max_minute / day_minutes))

        val_mae = None
        for pass_idx, window in enumerate(window_sizes):
            # Slice data to the oldest 'window' minutes.
            # start_minute is the more-recent edge; after re-indexing it becomes key 0.
            # We pass slice_now = now_utc - start_minute to train() so that the
            # re-indexed key 0 maps to the correct wall-clock time, keeping
            # time-of-day and day-of-week features accurate for this slice.
            start_minute = max_minute - window
            slice_now = now_utc - timedelta(minutes=start_minute)
            load_slice = self._slice_data_dict(load_minutes, start_minute, max_minute)
            pv_slice = self._slice_data_dict(pv_minutes, start_minute, max_minute)
            temp_slice = self._slice_data_dict(temp_minutes, start_minute, max_minute)
            import_slice = self._slice_data_dict(import_rates, start_minute, max_minute)
            export_slice = self._slice_data_dict(export_rates, start_minute, max_minute)

            self.log("ML Predictor: Curriculum pass {}/{}: window={:.1f} days ({} hours of data)".format(pass_idx + 1, total_passes, window / day_minutes, window // 60))

            pass_mae = self.train(
                load_slice,
                slice_now,
                pv_minutes=pv_slice,
                temp_minutes=temp_slice,
                import_rates=import_slice,
                export_rates=export_slice,
                is_initial=(pass_idx == 0),
                epochs=epochs,
                time_decay_days=time_decay_days,
                patience=patience,
                validation_holdout_hours=validation_holdout_hours,
                norm_ema_alpha=norm_ema_alpha,
                huber_delta=huber_delta,
            )

            if pass_mae is None:
                self.log("Warn: ML Predictor: Curriculum pass {}/{} failed (insufficient data or training error) - skipping".format(pass_idx + 1, total_passes))
            else:
                val_mae = pass_mae
                self.log("ML Predictor: Curriculum pass {}/{} complete, val_mae={:.4f} kWh".format(pass_idx + 1, total_passes, val_mae))

        # Final pass: full dataset, standard holdout window
        self.log("ML Predictor: Curriculum final pass {}/{}: full dataset ({:.1f} days)".format(total_passes, total_passes, max_minute / day_minutes))
        final_mae = self.train(
            load_minutes,
            now_utc,
            pv_minutes=pv_minutes,
            temp_minutes=temp_minutes,
            import_rates=import_rates,
            export_rates=export_rates,
            is_initial=False,
            epochs=epochs,
            time_decay_days=time_decay_days,
            patience=patience,
            validation_holdout_hours=validation_holdout_hours,
            norm_ema_alpha=norm_ema_alpha,
            huber_delta=huber_delta,
        )

        if final_mae is not None:
            val_mae = final_mae

        self.log("ML Predictor: Curriculum training complete, final val_mae={}".format("{:.4f} kWh".format(val_mae) if val_mae is not None else "None (all passes failed)"))
        return val_mae

    def predict(self, load_minutes, now_utc, midnight_utc, pv_minutes=None, temp_minutes=None, import_rates=None, export_rates=None, exog_features=None):
        """
        Generate predictions for the next 48 hours using autoregressive approach.

        Raw 5-min data is aggregated into CHUNK_MINUTES aligned chunks. Each
        prediction step predicts one CHUNK_MINUTES chunk, then feeds that back
        into the lookback window. This reduces noise compared to 5-min resolution.

        Predictions are blended with historical daily patterns to prevent
        autoregressive drift.

        Args:
            load_minutes: Dict of {minute: kwh_per_5min} (positive=historical, pre-converted by load_ml_component)
            now_utc: Current UTC timestamp
            midnight_utc: Today's midnight UTC timestamp
            pv_minutes: Dict of {minute: kwh_per_5min} PV generation (positive=historical, negative=future per-step)
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

        # Data is already in per-5-min energy format (pre-converted in load_ml_component)
        energy_per_step = load_minutes
        pv_energy_per_step = pv_minutes if pv_minutes else {}
        temp_values = temp_minutes if temp_minutes else {}
        import_rate_values = import_rates if import_rates else {}
        export_rate_values = export_rates if export_rates else {}

        if not energy_per_step:
            self.log("Warn: ML Predictor: No load data available for prediction")
            return {}

        # Compute historical daily patterns at CHUNK_MINUTES resolution for blending
        # Returns 7 DOW-specific patterns (falling back to global when data is sparse)
        daily_patterns = self._compute_daily_pattern(energy_per_step, now_utc)

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
            day_of_year = target_time.timetuple().tm_yday
            time_features = self._create_time_features(minute_of_day, day_of_week, day_of_year)

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

            # Normalize and forward pass (inference: no dropout)
            features_norm = self._normalize_features(features.reshape(1, -1), fit=False)
            pred_norm, _, _, _ = self._forward(features_norm)
            pred_energy = self._denormalize_predictions(pred_norm[0])

            # Apply physical constraints
            pred_energy = self._clip_predictions(pred_energy)
            model_pred = float(pred_energy[0])

            # Blend with historical daily pattern for this day-of-week (CHUNK_MINUTES slot)
            slot = (minute_of_day // CHUNK_MINUTES) * CHUNK_MINUTES
            hist_value = daily_patterns[day_of_week].get(slot, model_pred)

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
                "validation_bias": float(self.validation_bias) if self.validation_bias is not None else None,
                "epochs_trained": self.epochs_trained,
                "learning_rate": self.learning_rate,
                "max_load_kw": self.max_load_kw,
                "dropout_rate": self.dropout_rate,
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
            self.validation_bias = metadata.get("validation_bias", None)
            self.epochs_trained = metadata.get("epochs_trained", 0)
            # Restore dropout rate; fall back to 0.1 for models saved before this feature
            self.dropout_rate = metadata.get("dropout_rate", 0.1)

            self.model_initialized = True

            self.log(
                "ML Predictor: Model loaded from {} (trained {}, val_mae={:.4f}, val_bias={:+.4f})".format(
                    filepath, self.training_timestamp.strftime("%Y-%m-%d %H:%M") if self.training_timestamp else "unknown", self.validation_mae if self.validation_mae else 0, self.validation_bias if self.validation_bias is not None else 0
                )
            )
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
