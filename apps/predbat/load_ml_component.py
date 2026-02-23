# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# ML Load Forecaster Component - ComponentBase wrapper for LoadPredictor
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""ML load forecasting component integrating LoadPredictor with PredBat.

ComponentBase wrapper that manages the LoadPredictor lifecycle including
data fetching, periodic training/fine-tuning, prediction generation, and
status sensor publishing. Retrains every 2 hours and updates predictions
every 30 minutes.
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from component_base import ComponentBase
from utils import get_now_from_cumulative, dp2, dp4, minute_data
from load_predictor import LoadPredictor, MODEL_VERSION
from const import TIME_FORMAT, PREDICT_STEP
import json
import traceback
import numpy as np

# Training intervals
RETRAIN_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours between training cycles
PREDICTION_INTERVAL_SECONDS = 30 * 60  # 30 minutes between predictions

# Database schema version - increment when the saved format changes to force a clean rebuild
DATABASE_VERSION = 1


class LoadMLComponent(ComponentBase):
    """
    ML Load Forecaster component that predicts household load for the next 48 hours.

    This component:
    - Fetches load history from configured sensor
    - Optionally fills gaps using load_power sensor
    - Subtracts configured sensors (e.g., car charging) from load
    - Trains/fine-tunes an MLP model on historical load data
    - Generates predictions in the same format as load_forecast
    - Falls back to empty predictions when validation fails or model is stale
    """

    def initialize(self, load_ml_enable, load_ml_source=True, load_ml_max_days_history=28, load_ml_database_days=90):
        """
        Initialize the ML load forecaster component.

        Args:
            load_ml_enable: Whether ML forecasting is enabled
            load_ml_source: Whether using ML as the data source is enabled or not
            load_ml_max_days_history: Maximum number of days of load history to fetch
            load_ml_database_days: Number of days of historical data to store in the database, maximum length for training
        """

        self.ml_enable = load_ml_enable
        self.ml_source = load_ml_source
        self.ml_load_sensor = self.get_arg("load_today", default=[], indirect=False)
        self.ml_load_power_sensor = self.get_arg("load_power", default=[], indirect=False)
        self.ml_pv_sensor = self.get_arg("pv_today", default=[], indirect=False)
        self.ml_subtract_sensors = self.get_arg("car_charging_energy", default=[], indirect=False)
        self.car_charging_hold = self.get_arg("car_charging_hold", True)
        self.car_charging_threshold = float(self.get_arg("car_charging_threshold", 6.0)) / 60.0
        self.car_charging_energy_scale = self.get_arg("car_charging_energy_scale", 1.0)
        self.car_charging_rate = float(self.get_arg("car_charging_rate", 7.5)) / 60.0

        self.ml_learning_rate = 0.001
        self.ml_epochs_initial = 100
        self.ml_epochs_update = 20
        self.ml_min_days = 1
        self.ml_validation_threshold = 2.0
        self.ml_time_decay_days = 30
        self.ml_max_load_kw = 50.0
        self.ml_max_model_age_hours = 48
        self.ml_weight_decay = 0.01
        self.ml_max_days_history = load_ml_max_days_history
        self.load_ml_database_days = load_ml_database_days
        self.ml_validation_holdout_hours = 24
        self.ml_epochs_patience = 5

        # Data state
        self.load_data = None
        self.load_data_age_days = 0
        self.pv_data = None
        self.temperature_data = None
        self.import_rates_data = None
        self.export_rates_data = None
        self.data_ready = False
        self.data_lock = asyncio.Lock()
        self.last_data_fetch = None

        # Model state
        self.predictor = None
        self.model_valid = False
        self.model_status = "not_initialized"
        self.last_train_time = None
        self.initial_training_done = False
        self.database_history_loaded = False

        # Predictions cache
        self.current_predictions = {}

        # Model and database file paths
        self.model_filepath = None
        self.database_filepath = None

        # Validate configuration
        if self.ml_enable and not self.ml_load_sensor:
            self.log("Error: ML Component: ml_load_sensor must be configured when ml_enable is True")
            self.ml_enable = False

        # Initialize predictor
        self._init_predictor()

    def _init_predictor(self):
        """Initialize or reinitialize the predictor."""
        self.predictor = LoadPredictor(log_func=self.log, learning_rate=self.ml_learning_rate, max_load_kw=self.ml_max_load_kw, weight_decay=self.ml_weight_decay)

        # Determine model save path
        if self.config_root:
            self.model_filepath = os.path.join(self.config_root, "predbat_ml_model.npz")
            self.database_filepath = os.path.join(self.config_root, "predbat_ml_history.npz")
        else:
            self.model_filepath = None
            self.database_filepath = None

        # Try to load existing model
        if self.model_filepath and os.path.exists(self.model_filepath):
            load_success = self.predictor.load(self.model_filepath)
            if load_success:
                self.log("ML Component: Loaded existing model")
                # Check if model is still valid
                is_valid, reason = self.predictor.is_valid(validation_threshold=self.ml_validation_threshold, max_age_hours=self.ml_max_model_age_hours)
                if is_valid:
                    self.model_valid = True
                    self.model_status = "active"
                    self.initial_training_done = True
                else:
                    self.log("ML Component: Loaded model is invalid ({}), will retrain".format(reason))
                    self.model_status = "fallback_" + reason
            else:
                # Model load failed (version mismatch, architecture change, etc.)
                # Reinitialize predictor to ensure clean state
                self.log("ML Component: Failed to load model, reinitializing predictor")
                self.predictor = LoadPredictor(log_func=self.log, learning_rate=self.ml_learning_rate, max_load_kw=self.ml_max_load_kw, weight_decay=self.ml_weight_decay)

    def get_from_incrementing(self, data, index, step, backwards=True):
        """
        Get a single value from an incrementing series e.g. kWh today -> kWh this minute
        """
        while index < 0:
            index += 24 * 60
        if backwards:
            return max(data.get(index, 0) - data.get(index + step, 0), 0)
        else:
            return max(data.get(index + step, 0) - data.get(index, 0), 0)

    def car_subtraction(self, load_cumulative, car_cumulative, step=PREDICT_STEP, interpolate_gaps=True, max_gap_minutes=60, smoothing_window=3):
        """
        Improved car charging subtraction that handles timing misalignment and gaps.

        Args:
            load_cumulative: Dict of {minute: cumulative_kwh} for load (backwards format)
            car_cumulative: Dict of {minute: cumulative_kwh} for car charging (backwards format)
            step: Step size in minutes (default 5)
            interpolate_gaps: Whether to interpolate missing car data
            max_gap_minutes: Maximum gap to interpolate (minutes)
            smoothing_window: Number of steps for smoothing car data

        Returns:
            Dict of {minute: cumulative_kwh} with improved car subtraction
        """
        if not car_cumulative:
            return load_cumulative
        if not load_cumulative:
            return load_cumulative

        # Step 1: Fill gaps in car charging data with interpolation
        car_filled = dict(car_cumulative)

        if interpolate_gaps:
            car_minutes = sorted(car_cumulative.keys())
            for i in range(len(car_minutes) - 1):
                current_minute = car_minutes[i]
                next_minute = car_minutes[i + 1]
                gap = next_minute - current_minute

                if gap > step and gap <= max_gap_minutes:
                    # Interpolate linearly between the two points
                    current_value = car_cumulative[current_minute]
                    next_value = car_cumulative[next_minute]

                    for m in range(current_minute + step, next_minute, step):
                        # Linear interpolation
                        alpha = (m - current_minute) / (next_minute - current_minute)
                        car_filled[m] = current_value + alpha * (next_value - current_value)

        # Step 2: Calculate per-step deltas with smoothing
        car_deltas = {}
        load_deltas = {}

        max_minute = max(load_cumulative.keys())
        max_minute = (max_minute // step) * step  # Align to step intervals

        for minute in range(0, max_minute + step, step):
            # Calculate load delta
            load_delta = self.get_from_incrementing(load_cumulative, minute, step, backwards=True)
            load_deltas[minute] = max(0, load_delta)

            # Calculate car delta
            car_delta = self.get_from_incrementing(car_filled, minute, step, backwards=True)
            car_deltas[minute] = max(0, car_delta)

        # Step 3: Apply smoothing to car deltas to handle timing misalignment
        car_deltas_smoothed = {}
        car_minutes_sorted = sorted(car_deltas.keys())

        for i, minute in enumerate(car_minutes_sorted):
            # Collect values within smoothing window
            values = []
            for offset in range(-smoothing_window // 2, smoothing_window // 2 + 1):
                idx = i + offset
                if 0 <= idx < len(car_minutes_sorted):
                    other_minute = car_minutes_sorted[idx]
                    values.append(car_deltas[other_minute])

            # Use median for robust smoothing (less affected by outliers)
            if values:
                car_deltas_smoothed[minute] = np.median(values)
            else:
                car_deltas_smoothed[minute] = car_deltas[minute]

        # Step 4: Adaptive subtraction - look at nearby intervals for better matching
        result = {}
        total_energy = 0.0
        search_window = 2  # Look ±2 intervals for best match

        for minute in range(max_minute, -step, -step):
            load_delta = load_deltas.get(minute, 0.0)

            # Find best matching car delta within search window
            best_car_delta = 0.0

            # Check current and nearby minutes
            for offset in range(-search_window * step, (search_window + 1) * step, step):
                check_minute = minute + offset
                if check_minute in car_deltas_smoothed:
                    car_delta_candidate = car_deltas_smoothed[check_minute]

                    # Prefer car delta that's close to but not exceeding load delta
                    if car_delta_candidate <= load_delta:
                        # Good match - car charging doesn't exceed load
                        if car_delta_candidate > best_car_delta:
                            best_car_delta = car_delta_candidate
                    elif best_car_delta == 0:
                        # Only use exceeding value if we have no better option
                        # Take minimum to avoid negative result
                        best_car_delta = min(car_delta_candidate, load_delta * 0.95)

            # Subtract car charging from load
            adjusted_delta = max(0.0, load_delta - best_car_delta)

            total_energy += adjusted_delta
            result[minute] = total_energy
        return result

    async def _fetch_load_data(self):
        """
        Fetch and process load data from configured sensors.

        Returns:
            Tuple of (load_minutes_dict, age_days, load_minutes_now, pv_data, temperature_data, import_rates_data, export_rates_data)
            or (None, 0, 0, None, None, None, None) on failure
        """
        if not self.ml_load_sensor:
            self.log("Warn: ML Component: No load sensor configured, cannot fetch load data")
            return None, 0, 0, None, None, None, None

        try:
            # Determine how many days of history to fetch, up to N days back
            days_to_fetch = max(self.ml_max_days_history, self.ml_min_days)

            # Fetch load sensor history
            self.log("ML Component: Fetching {} days of load history from {}".format(days_to_fetch, self.ml_load_sensor))

            load_minutes, load_minutes_age = self.base.minute_data_load(self.now_utc, "load_today", days_to_fetch, required_unit="kWh", load_scaling=self.get_arg("load_scaling", 1.0), interpolate=True, pad=False)
            if not load_minutes:
                self.log("Warn: ML Component: Failed to convert load history to minute data")
                return None, 0, 0, None, None, None, None
            days_to_fetch = min(days_to_fetch, load_minutes_age)

            if self.get_arg("load_power", default=None, indirect=False) and self.get_arg("load_power_fill_enable", True):
                load_power_data, load_minutes_age = self.base.minute_data_load(self.now_utc, "load_power", days_to_fetch, required_unit="W", load_scaling=1.0, interpolate=True, pad=False)
                load_minutes = self.base.fill_load_from_power(load_minutes, load_power_data)

            car_charging_energy = {}
            if self.get_arg("car_charging_energy", default=None, indirect=False):
                car_charging_energy = self.base.minute_data_import_export(days_to_fetch, self.now_utc, "car_charging_energy", scale=self.car_charging_energy_scale, required_unit="kWh", pad=False)

            max_minute = max(load_minutes.keys()) if load_minutes else 0
            max_minute = (max_minute // 5) * 5  # Align to 5-minute intervals
            load_minutes_new = {}

            if self.car_charging_hold and car_charging_energy:
                # Use improved car subtraction method that handles timing misalignment and gaps
                self.log("ML Component: Applying improved car charging subtraction with hold and interpolation")
                load_minutes_new = self.car_subtraction(load_minutes, car_charging_energy, step=PREDICT_STEP, interpolate_gaps=True, max_gap_minutes=60, smoothing_window=3)
                self.log("ML Component: Car charging subtraction applied, {} data points after subtraction".format(len(load_minutes_new)))
            else:
                # Filter car charging based on threshold if enabled
                total_load_energy = 0
                for minute in range(max_minute, -PREDICT_STEP, -PREDICT_STEP):
                    car_delta = 0.0
                    if self.car_charging_hold:
                        load_now = self.get_from_incrementing(load_minutes, minute, PREDICT_STEP, backwards=True)
                        if load_now >= self.car_charging_threshold * PREDICT_STEP:
                            car_delta = self.car_charging_rate * PREDICT_STEP

                    # Work out load delta and subtract car delta
                    load_delta = self.get_from_incrementing(load_minutes, minute, PREDICT_STEP, backwards=True)
                    load_delta = max(0.0, load_delta - car_delta)

                    # Store at 5-minute intervals (ML only uses PREDICT_STEP data)
                    load_minutes_new[minute] = dp4(total_load_energy + load_delta)
                    total_load_energy += load_delta

            # Get current cumulative load value (excludes car) before converting to per-step
            load_minutes_now = get_now_from_cumulative(load_minutes_new, self.minutes_now, backwards=True)

            # Convert cumulative load data to per-5-min energy for the predictor
            # Positive keys: historical, minute 0 = most recent; energy = delta per PREDICT_STEP interval
            load_max_minute = max(load_minutes_new.keys()) if load_minutes_new else 0
            load_energy_per_step = {}
            for m in range(0, load_max_minute, PREDICT_STEP):
                energy = self.get_from_incrementing(load_minutes_new, m, PREDICT_STEP, backwards=True)
                load_energy_per_step[m] = dp4(energy)
            load_minutes_new = load_energy_per_step

            # PV Data
            if self.ml_pv_sensor:
                pv_data_hist, pv_minutes_age = self.base.minute_data_load(self.now_utc, "pv_today", days_to_fetch, required_unit="kWh", load_scaling=1.0, interpolate=True, pad=False)
                days_to_fetch = min(days_to_fetch, pv_minutes_age)
            else:
                pv_data_hist = {}

            # Build per-minute cumulative PV history resampled to 30-min aligned boundaries.
            # This normalises the data regardless of how frequently the sensor is sampled.
            pv_data_cumulative = {}
            alignment = self.minutes_now % 30  # e.g. we are 17 minutes past a 30 minute boundary
            current_value = 0
            for minute_align in range(max_minute - alignment, -30, -30):
                new_value = pv_data_hist.get(minute_align, current_value)
                delta_per_minute = max(new_value - current_value, 0) / 30.0
                for m in range(29, -1, -1):
                    minute = minute_align + m
                    if minute >= 0:
                        pv_data_cumulative[minute] = dp4(new_value - (delta_per_minute * m))
                current_value = new_value

            # Convert cumulative per-minute PV history to per-5-min energy
            # Positive keys: historical; minute 0 = most recent
            pv_data = {}
            pv_hist_max = max(pv_data_cumulative.keys()) if pv_data_cumulative else 0
            for m in range(0, pv_hist_max, PREDICT_STEP):
                energy = self.get_from_incrementing(pv_data_cumulative, m, PREDICT_STEP, backwards=True)
                pv_data[m] = dp4(energy)

            pv_forecast_minute, pv_forecast_minute10 = self.base.fetch_pv_forecast()
            # Add future PV forecast as per-5-min energy with negative keys (negative = future)
            # key -5 = first future step, -10 = second, etc.
            if pv_forecast_minute:
                max_minute = max(pv_forecast_minute.keys()) + PREDICT_STEP
                for minute in range(self.minutes_now + PREDICT_STEP, max_minute, PREDICT_STEP):
                    step_energy = pv_forecast_minute.get(minute, 0.0)
                    pv_data[-(minute - self.minutes_now)] = dp4(step_energy)

            # Temperature predictions
            temp_entity = "sensor." + self.prefix + "_temperature"
            temperature_info = self.get_state_wrapper(temp_entity, attribute="results")
            temperature_data = {}
            if isinstance(temperature_info, dict):
                data_array = []
                for key, value in temperature_info.items():
                    data_array.append({"state": value, "last_updated": key})

                # Load data from past and future predictions, base backwards around now_utc
                # We also get the last N days in the past to help the model learn the daily pattern
                temperature_data, _ = minute_data(
                    data_array,
                    days_to_fetch,
                    self.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=True,
                    divide_by=1.0,
                    scale=1.0,
                )
                self.log("ML Temperature data points: {}".format(len(temperature_data)))

            # Import and export prices
            import_entity = self.prefix + ".rates"
            export_entity = self.prefix + ".rates_export"
            import_rates_future = self.get_state_wrapper(import_entity, attribute="results")
            import_rates_data = {}
            if import_rates_future:
                data_array = []
                for key, value in import_rates_future.items():
                    data_array.append({"state": value, "last_updated": key})
                import_rates_data, _ = minute_data(
                    data_array,
                    days_to_fetch,
                    self.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=1.0,
                )
            export_rates_future = self.get_state_wrapper(export_entity, attribute="results")
            export_rates_data = {}
            if export_rates_future:
                data_array = []
                for key, value in export_rates_future.items():
                    data_array.append({"state": value, "last_updated": key})
                export_rates_data, _ = minute_data(
                    data_array,
                    days_to_fetch,
                    self.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=1.0,
                )
            # try to retrieve import and export rate history to detect rate-based load patterns
            import_rates_history = self.base.minute_data_import_export(days_to_fetch, self.now_utc, import_entity, scale=1.0, increment=False, smoothing=False, pad=False)
            export_rates_history = self.base.minute_data_import_export(days_to_fetch, self.now_utc, export_entity, scale=1.0, increment=False, smoothing=False, pad=False)

            # Merge import_rates_history with import_rates_data
            if import_rates_history is None:
                import_rates_history = {}
            if export_rates_history is None:
                export_rates_history = {}

            for minute in range(0, int(days_to_fetch * 24 * 60), PREDICT_STEP):
                # Note we offset by 2 minutes to allow predbat to have published the rate for this period
                value = import_rates_history.get(minute + 2, None)
                if value is not None and minute not in import_rates_data:
                    import_rates_data[minute] = value
                value = export_rates_history.get(minute + 2, None)
                if value is not None and minute not in export_rates_data:
                    export_rates_data[minute] = value

            self.log("ML Component: Fetched {} load data points, {:.1f} days of history".format(len(load_minutes_new), days_to_fetch))
            if 0:
                with open("ml_load_debug.json", "w") as f:
                    json.dump(
                        {
                            "now_utc": self.now_utc.isoformat(),
                            "midnight_utc": self.midnight_utc.isoformat(),
                            "minutes_now": self.minutes_now,
                            "age_days": days_to_fetch,
                            "load_minutes_now": load_minutes_now,
                            "load_minutes": load_minutes_new,
                            "original_load_minutes": load_minutes,
                            "car_charging_energy": car_charging_energy,
                            "pv_data": pv_data,
                            "temperature_data": temperature_data,
                            "import_rates": import_rates_data,
                            "export_rates": export_rates_data,
                        },
                        f,
                        indent=2,
                    )
            return load_minutes_new, days_to_fetch, load_minutes_now, pv_data, temperature_data, import_rates_data, export_rates_data

        except Exception as e:
            self.log("Error: ML Component: Failed to fetch load data: {}".format(e))
            self.log("Error: ML Component: {}".format(traceback.format_exc()))
            return None, 0, 0, None, None, None, None

    @staticmethod
    def _shift_channel(d, elapsed_minutes, max_minutes):
        """
        Shift historical {minute: value} keys (k >= 0) forward by elapsed time, dropping entries
        that fall beyond max_minutes. Negative keys (future data) are not shifted to avoid
        promoting future entries into persisted history.
        """
        if not d or elapsed_minutes <= 0:
            return d
        shift = round(elapsed_minutes / PREDICT_STEP) * PREDICT_STEP
        shifted = {}
        for k, v in d.items():
            # Only shift historical entries; future (negative) keys are ignored here and should be re-fetched
            if k < 0:
                continue
            new_k = k + shift
            if new_k < max_minutes:
                shifted[new_k] = v
        return shifted

    @staticmethod
    def _merge_channel(old, new):
        """Merge two channel dicts, with new values taking priority over old."""
        if old:
            merged = dict(old)
            merged.update(new)
            return merged
        return new

    def _recompute_age_days(self):
        """Recompute load_data_age_days from the actual extent of load_data keys."""
        if self.load_data:
            self.load_data_age_days = max(self.load_data.keys()) // (24 * 60)

    def _shift_fetch_data(self):
        """
        Shift existing in-memory channel dicts by the time elapsed since the last fetch to keep keys anchored to 'minutes ago from now'.
        """
        max_history_minutes = self.load_ml_database_days * 24 * 60 if self.load_ml_database_days else self.ml_max_days_history * 24 * 60

        if self.last_data_fetch:
            elapsed = (self.now_utc - self.last_data_fetch).total_seconds() / 60.0
            self.load_data = self._shift_channel(self.load_data, elapsed, max_history_minutes)
            self.pv_data = self._shift_channel(self.pv_data, elapsed, max_history_minutes)
            self.temperature_data = self._shift_channel(self.temperature_data, elapsed, max_history_minutes)
            self.import_rates_data = self._shift_channel(self.import_rates_data, elapsed, max_history_minutes)
            self.export_rates_data = self._shift_channel(self.export_rates_data, elapsed, max_history_minutes)
            self._recompute_age_days()
            # Update the reference time so the same elapsed interval is not applied more than once
            self.last_data_fetch = self.now_utc

    def _merge_fetch_data(self, load_data, age_days, load_minutes_now, pv_data, temperature_data, import_rates_data, export_rates_data):
        """
        Merge newly fetched data with existing in-memory data
        """
        self.load_data = self._merge_channel(self.load_data, load_data)
        self.pv_data = self._merge_channel(self.pv_data, pv_data) if pv_data else self.pv_data
        self.temperature_data = self._merge_channel(self.temperature_data, temperature_data) if temperature_data else self.temperature_data
        self.import_rates_data = self._merge_channel(self.import_rates_data, import_rates_data) if import_rates_data else self.import_rates_data
        self.export_rates_data = self._merge_channel(self.export_rates_data, export_rates_data) if export_rates_data else self.export_rates_data
        self._recompute_age_days()
        self.load_minutes_now = load_minutes_now
        self.last_data_fetch = self.now_utc
        self.data_ready = True
        self.ml_validation_holdout_hours = 48 if self.load_data_age_days > 3 else 24

    def get_current_prediction(self):
        """
        Returns the current ML load predictions.

        Output format:
            Dict of {minute: cumulative_kwh}
        """
        return self.current_predictions

    def _get_predictions(self, now_utc, midnight_utc, exog_features=None):
        """
        Get current predictions for integration with load_forecast.

        Called from fetch.py to retrieve ML predictions.

        Args:
            now_utc: Current UTC timestamp
            midnight_utc: Today's midnight UTC timestamp
            exog_features: Optional dict with future exogenous data

        Returns:
            Dict of {minute: cumulative_kwh} or empty dict on fallback
        """
        if not self.ml_enable:
            self.log("ML Component: ML forecasting disabled, returning empty predictions")
            return {}

        if not self.data_ready:
            self.log("ML Component: No load data available for prediction")
            return {}

        if not self.model_valid:
            self.log("ML Component: Model not valid ({}), returning empty predictions".format(self.model_status))
            return {}

        # Generate predictions using current model
        try:
            self.log("ML Component: Generating predictions load data age {:.1f} days, {} data points".format(self.load_data_age_days, len(self.load_data) if self.load_data else 0))
            predictions = self.predictor.predict(self.load_data, now_utc, midnight_utc, pv_minutes=self.pv_data, temp_minutes=self.temperature_data, import_rates=self.import_rates_data, export_rates=self.export_rates_data, exog_features=exog_features)

            if predictions:
                self.current_predictions = predictions
                self.log("ML Component: Generated {} predictions (total {:.2f} kWh over 48h)".format(len(predictions), max(predictions.values()) if predictions else 0))
            else:
                self.log("ML Component: Predictor returned no predictions, returning previous dict")

            return self.current_predictions

        except Exception as e:
            self.log("Error: ML Component: Prediction failed: {}".format(e))
            return {}

    async def run(self, seconds, first):
        """
        Main component loop - handles data fetching, training and prediction cycles.

        Args:
            seconds: Seconds since component start
            first: True if this is the first run

        Returns:
            True if successful, False otherwise
        """
        if not self.ml_enable:
            self.api_started = True
            return True

        # Determine if training is needed
        is_initial = not self.initial_training_done

        # Fetch fresh load data periodically (every 15 minutes)
        should_fetch = first or ((seconds % PREDICTION_INTERVAL_SECONDS) == 0)
        should_train = first or ((seconds % RETRAIN_INTERVAL_SECONDS) == 0)

        if not self.database_history_loaded and self.load_ml_database_days:
            async with self.data_lock:
                await self.load_database_history()
            should_fetch = True
            should_train = True
        elif is_initial:
            # Initial run, need to fetch data and train model before we can provide predictions
            should_train = True
            should_fetch = True
        elif should_train:
            # Training requires fetching
            should_fetch = True

        if should_fetch:
            # Shift existing data to keep it anchored to 'minutes ago from now' before fetching new data, so that we can merge them and preserve historical depth even if the fetch returns limited history
            async with self.data_lock:
                self._shift_fetch_data()
                load_data, age_days, load_minutes_now, pv_data, temperature_data, import_rates_data, export_rates_data = await self._fetch_load_data()
                if load_data:
                    self._merge_fetch_data(load_data, age_days, load_minutes_now, pv_data, temperature_data, import_rates_data, export_rates_data)
                else:
                    self.log("Warn: ML Component: Failed to fetch load data, no data was returned.")
            self.log("ML Component: Data fetch completed, load data age {:.1f} days, {} data points".format(self.load_data_age_days, len(self.load_data) if self.load_data else 0))

        # Check if we have data
        if not self.data_ready:
            self.log("ML Component: Waiting for load data from sensors")
            return True  # Not an error, just waiting

        # Check if we have enough data
        if self.load_data_age_days < self.ml_min_days:
            self.model_status = "insufficient_data"
            self.model_valid = False
            self.log("ML Component: Insufficient data ({:.1f} days, need {})".format(self.load_data_age_days, self.ml_min_days))
            return True

        if is_initial:
            self.log("ML Component: Starting initial training")
        elif should_train:
            self.log("ML Component: Starting fine-tune training (2h interval)")

        if should_train:
            await self._do_training(is_initial)

        # Update model validity status
        self._update_model_status()

        if should_fetch:
            if self.base.prediction_started:
                self.log("ML Component: Waiting for current prediction cycle to complete before generating new ML predictions")
                while self.base.prediction_started:
                    await asyncio.sleep(0.5)
            try:
                self.base.prediction_started = True
                self.log("ML Component: Generating predictions for load_forecast integration")
                self._get_predictions(self.now_utc, self.midnight_utc)
            except:
                self.log("Error: ML Component: Failed to generate predictions: {}".format(traceback.format_exc()))
            finally:
                self.base.prediction_started = False

            # Publish entity with current state
            self._publish_entity()
            self.log("ML Component: Prediction cycle completed")
            # Write database if enabled
            if self.load_ml_database_days:
                await self.save_database_history()

        self.update_success_timestamp()
        return True

    async def save_database_history(self):
        """Save historical channel data to a compact compressed binary file.

        Each channel (load, pv, temp, import_rate, export_rate) is stored as a dense
        float32 array indexed by step number (index i = minute i*PREDICT_STEP from now).
        A JSON metadata blob captures the save timestamp and schema version so that on
        reload we can shift the arrays to align them with the current time.

        Only historical (positive-key) data is persisted; future forecast values
        (negative keys in pv_data) are always re-fetched fresh.
        """
        if not self.database_filepath or not self.load_data:
            return

        max_steps = self.load_ml_database_days * 24 * 60 // PREDICT_STEP

        def dict_to_array(data_dict):
            arr = np.zeros(max_steps, dtype=np.float32)
            if data_dict:
                for minute, value in data_dict.items():
                    # Only persist historical data (non-negative integer keys)
                    if isinstance(minute, int) and minute >= 0:
                        idx = minute // PREDICT_STEP
                        if 0 <= idx < max_steps:
                            arr[idx] = float(value)
            return arr

        total_steps = len(self.load_data) if self.load_data else 0

        metadata = json.dumps(
            {
                "version": DATABASE_VERSION,
                "saved_utc": self.now_utc.isoformat(),
                "step_minutes": PREDICT_STEP,
                "n_steps": total_steps,
                "age_days": float(self.load_data_age_days),
            }
        )

        save_kwargs = dict(
            metadata_json=np.array(metadata),
            load=dict_to_array(self.load_data),
            pv=dict_to_array(self.pv_data),
            temp=dict_to_array(self.temperature_data),
            import_rate=dict_to_array(self.import_rates_data),
            export_rate=dict_to_array(self.export_rates_data),
        )
        np.savez_compressed(self.database_filepath, **save_kwargs)
        self.log("ML Component: Saved {} steps ({} days) of history to {}".format(total_steps, self.load_data_age_days, self.database_filepath))

    async def load_database_history(self):
        """Load and time-shift historical channel data from the compressed binary database.

        The arrays are shifted rightward by the number of 5-min steps that have elapsed
        since the file was saved, so that old data appears at the correct historical offset
        relative to the current time.  The fresh sensor fetch that follows will overwrite
        the most recent portion of each channel.
        """
        self.database_history_loaded = True

        if not self.database_filepath or not os.path.exists(self.database_filepath):
            self.log("ML Component: No database history file found, starting fresh")
            return

        try:
            # np.load is blocking disk IO - run in executor so the event loop stays alive
            data = np.load(self.database_filepath, allow_pickle=False)
            metadata = json.loads(str(data["metadata_json"]))

            version = metadata.get("version", 0)
            if version != DATABASE_VERSION:
                self.log("Warn: ML Component: Database version mismatch (saved={}, current={}), discarding".format(version, DATABASE_VERSION))
                return

            step_minutes = metadata.get("step_minutes", PREDICT_STEP)
            if step_minutes != PREDICT_STEP:
                self.log("Warn: ML Component: Database step size mismatch (saved={}min, current={}min), discarding".format(step_minutes, PREDICT_STEP))
                return

            saved_utc = datetime.fromisoformat(metadata["saved_utc"])
            age_days = float(metadata.get("age_days", 0))

            max_steps = self.load_ml_database_days * 24 * 60 // PREDICT_STEP

            def array_to_dict(arr):
                """Reconstruct a sparse {minute: value} dict with keys as stored"""
                result = {}
                for i in range(len(arr)):
                    val = float(arr[i])
                    if val != 0.0:
                        result[i * PREDICT_STEP] = val
                return result

            self.load_data = array_to_dict(data["load"])
            self.pv_data = array_to_dict(data["pv"])
            self.temperature_data = array_to_dict(data["temp"])
            self.import_rates_data = array_to_dict(data["import_rate"])
            self.export_rates_data = array_to_dict(data["export_rate"])
            self.load_data_age_days = age_days
            # Restore last_data_fetch to the save time so that run()'s _shift() will
            # compute the correct elapsed time and apply the shift exactly once.
            self.last_data_fetch = saved_utc

            if self.load_data:
                self.data_ready = True

            elapsed_minutes = (self.now_utc - saved_utc).total_seconds() / 60.0
            self.log("ML Component: Loaded database history: {} load points, {:.1f}h elapsed since save, age {:.1f} days (shift will be applied on next fetch)".format(len(self.load_data), elapsed_minutes / 60.0, self.load_data_age_days))

        except Exception as e:
            self.log("Warn: ML Component: Failed to load database history: {} - {}".format(e, traceback.format_exc()))
            # Reset any partial state
            self.load_data = None
            self.pv_data = None
            self.temperature_data = None
            self.import_rates_data = None
            self.export_rates_data = None
            self.data_ready = False

    async def _do_training(self, is_initial):
        """
        Perform model training.

        Args:
            is_initial: True for full training, False for fine-tuning
        """
        # Snapshot data under the lock so we can release it before the CPU-bound
        # training call.  predictor.train() can take 30-120 seconds on slow hardware
        # and must NOT run while holding data_lock or the asyncio event loop will freeze.
        async with self.data_lock:
            if not self.load_data:
                self.log("Warn: ML Component: No data for training")
                return

            if self.load_data_age_days < 3:
                self.log("Warn: ML Component: Training with only {} days of data, recommend 3+ days for better accuracy".format(self.load_data_age_days))

            # Shallow copies are sufficient - predictor.train() only reads the dicts
            load_data_snap = dict(self.load_data)
            pv_data_snap = dict(self.pv_data) if self.pv_data else {}
            temp_data_snap = dict(self.temperature_data) if self.temperature_data else {}
            import_rates_snap = dict(self.import_rates_data) if self.import_rates_data else {}
            export_rates_snap = dict(self.export_rates_data) if self.export_rates_data else {}
            now_utc_snap = self.now_utc
            epochs = self.ml_epochs_initial if is_initial else self.ml_epochs_update
            time_decay = min(self.ml_time_decay_days, self.load_data_age_days)
            holdout_hours = self.ml_validation_holdout_hours
            patience = self.ml_epochs_patience
        # Lock released - event loop is free during training

        try:
            # Run synchronous NumPy training in a thread-pool executor so the
            # asyncio event loop stays responsive throughout.
            val_mae = self.predictor.train(
                load_data_snap,
                now_utc_snap,
                pv_minutes=pv_data_snap,
                temp_minutes=temp_data_snap,
                import_rates=import_rates_snap,
                export_rates=export_rates_snap,
                is_initial=is_initial,
                epochs=epochs,
                time_decay_days=time_decay,
                validation_holdout_hours=holdout_hours,
                patience=patience,
            )

            if val_mae is not None:
                self.last_train_time = datetime.now(timezone.utc)
                self.initial_training_done = True

                # Check validation threshold
                if val_mae <= self.ml_validation_threshold:
                    self.model_valid = True
                    self.model_status = "active"
                    self.log("ML Component: Training successful, val_mae={:.4f} kWh".format(val_mae))
                else:
                    self.model_valid = False
                    self.model_status = "fallback_validation"
                    self.log("Warn: ML Component: Validation MAE ({:.4f}) exceeds threshold ({:.4f})".format(val_mae, self.ml_validation_threshold))

                # Save model (fast - no lock needed)
                if self.model_filepath:
                    self.predictor.save(self.model_filepath)
            else:
                self.log("Warn: ML Component: Training failed")

        except Exception as e:
            self.log("Error: ML Component: Training exception: {}".format(e))
            self.log("Error: " + traceback.format_exc())

    def _update_model_status(self):
        """Update model validity status based on current state."""
        if not self.predictor or not self.predictor.model_initialized:
            self.model_valid = False
            self.model_status = "not_initialized"
            return

        is_valid, reason = self.predictor.is_valid(validation_threshold=self.ml_validation_threshold, max_age_hours=self.ml_max_model_age_hours)

        if is_valid:
            self.model_valid = True
            self.model_status = "active"
        else:
            self.model_valid = False
            self.model_status = "fallback_" + reason

    def _publish_entity(self):
        """Publish the load_forecast_ml entity with current predictions."""
        # Convert predictions to timestamp format for entity
        results = {}
        reset_amount = 0
        load_today_h1 = 0
        load_today_h8 = 0
        load_today_now = 0
        power_today_now = 0
        power_today_h1 = 0
        power_today_h8 = 0
        # Future predictions
        if self.current_predictions:
            prev_value = 0
            for minute, value in self.current_predictions.items():
                timestamp = self.midnight_utc + timedelta(minutes=minute + self.minutes_now)
                timestamp_str = timestamp.strftime(TIME_FORMAT)
                # Reset at midnight
                if minute > 0 and ((minute + self.minutes_now) % (24 * 60) == 0):
                    reset_amount = value + self.load_minutes_now
                output_value = round(value - reset_amount + self.load_minutes_now, 4)
                results[timestamp_str] = output_value
                delta_value = (value - prev_value) / PREDICT_STEP * 60.0
                if minute == 0:
                    power_today_now = delta_value
                if minute == 60:
                    load_today_h1 = output_value
                    power_today_h1 = delta_value
                if minute == 60 * 8:
                    load_today_h8 = output_value
                    power_today_h8 = delta_value
                prev_value = value

        # Get model age
        model_age_hours = self.predictor.get_model_age_hours() if self.predictor else None

        # Calculate total predicted load
        total_kwh = max(self.current_predictions.values()) if self.current_predictions else 0

        self.dashboard_item(
            "sensor." + self.prefix + "_load_ml_forecast",
            state=self.model_status,
            attributes={
                "results": results,
                "friendly_name": "ML Load Forecast",
                "icon": "mdi:chart-line",
            },
            app="load_ml",
        )
        self.dashboard_item(
            "sensor." + self.prefix + "_load_ml_stats",
            state=round(total_kwh, 2),
            attributes={
                "load_today": dp2(self.load_minutes_now),
                "load_today_h1": dp2(load_today_h1),
                "load_today_h8": dp2(load_today_h8),
                "load_total": dp2(total_kwh),
                "power_today_now": dp2(power_today_now),
                "power_today_h1": dp2(power_today_h1),
                "power_today_h8": dp2(power_today_h8),
                "mae_kwh": round(self.predictor.validation_mae, 4) if self.predictor and self.predictor.validation_mae else None,
                "last_trained": self.last_train_time.isoformat() if self.last_train_time else None,
                "model_age_hours": round(model_age_hours, 1) if model_age_hours else None,
                "training_days": self.load_data_age_days,
                "status": self.model_status,
                "model_version": MODEL_VERSION,
                "epochs_trained": self.predictor.epochs_trained if self.predictor else 0,
                "friendly_name": "ML Load Stats",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:chart-line",
            },
            app="load_ml",
        )
