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
import traceback
import numpy as np

# Training intervals
RETRAIN_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours between training cycles
PREDICTION_INTERVAL_SECONDS = 30 * 60  # 30 minutes between predictions


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

    def initialize(self, load_ml_enable, load_ml_source=True, load_ml_max_days_history=28):
        """
        Initialize the ML load forecaster component.

        Args:
            load_ml_enable: Whether ML forecasting is enabled
            load_ml_source: Whether using ML as the data source is enabled or not
            load_ml_max_days_history: Maximum number of days of load history to use for training
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
        self.ml_validation_holdout_hours = 24

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

        # Predictions cache
        self.current_predictions = {}

        # Model file path
        self.model_filepath = None

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
        else:
            self.model_filepath = None

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

            load_minutes, load_minutes_age = self.base.minute_data_load(self.now_utc, "load_today", days_to_fetch, required_unit="kWh", load_scaling=self.get_arg("load_scaling", 1.0), interpolate=True)
            if not load_minutes:
                self.log("Warn: ML Component: Failed to convert load history to minute data")
                return None, 0, 0, None, None, None, None
            days_to_fetch = min(days_to_fetch, load_minutes_age)

            if self.get_arg("load_power", default=None, indirect=False) and self.get_arg("load_power_fill_enable", True):
                load_power_data, load_minutes_age = self.base.minute_data_load(self.now_utc, "load_power", days_to_fetch, required_unit="W", load_scaling=1.0, interpolate=True)
                load_minutes = self.base.fill_load_from_power(load_minutes, load_power_data)

            car_charging_energy = {}
            if self.get_arg("car_charging_energy", default=None, indirect=False):
                car_charging_energy = self.base.minute_data_import_export(days_to_fetch, self.now_utc, "car_charging_energy", scale=self.car_charging_energy_scale, required_unit="kWh")

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

            # Get current cumulative load value (excludes car)
            load_minutes_now = get_now_from_cumulative(load_minutes_new, self.minutes_now, backwards=True)

            # PV Data
            if self.ml_pv_sensor:
                pv_data_hist, pv_minutes_age = self.base.minute_data_load(self.now_utc, "pv_today", days_to_fetch, required_unit="kWh", load_scaling=1.0, interpolate=True)
                days_to_fetch = min(days_to_fetch, pv_minutes_age)
            else:
                pv_data_hist = {}

            pv_data = {}
            # The historical PV data is incrementing, but samples can be taken quite frequently.
            # As the future forecast is only in 30 minute slots aligned to 30 minute boundary
            # we should try to normalise the historical data otherwise future predictions will be very inaccurate when the historical data is sampled at a different frequency to the future predictions.
            # To do this we will take the historical PV data and resample it to 30 minute slots aligned to 30 minute boundary, we will then use the future forecast to fill in the gaps between the historical data and the future predictions.
            alignment = self.minutes_now % 30  # e.g. we are 17 minutes past a 30 minute boundary
            current_value = 0
            for minute_align in range(max_minute - alignment, -30, -30):
                new_value = pv_data_hist.get(minute_align, current_value)
                delta_per_minute = max(new_value - current_value, 0) / 30.0
                # Spread the delta evenly over the 30 minute period leading up to this point
                # Interpolate backwards from new_value at minute_align to current_value at minute_align-30
                for m in range(29, -1, -1):
                    minute = minute_align + m
                    if minute >= 0:
                        pv_data[minute] = dp4(new_value - (delta_per_minute * m))
                current_value = new_value

            pv_forecast_minute, pv_forecast_minute10 = self.base.fetch_pv_forecast()
            # PV Data has the historical PV data (minute is the number of minutes in the past)
            # PV forecast has the predicted PV generation for the next 24 hours (minute is the number of minutes from midnight forward
            # Combine the two into a new dict where negative minutes are in the future and positive in the past
            current_value = pv_data.get(0, 0)
            if pv_forecast_minute:
                max_minute = max(pv_forecast_minute.keys()) + PREDICT_STEP
                for minute in range(self.minutes_now + PREDICT_STEP, max_minute, PREDICT_STEP):
                    current_value += pv_forecast_minute.get(minute, 0.0)  # Add 0 if missing, not current_value
                    pv_data[-minute + self.minutes_now] = dp4(current_value)

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
            import_rates_history = self.base.minute_data_import_export(days_to_fetch, self.now_utc, import_entity, scale=1.0, increment=False, smoothing=False)
            export_rates_history = self.base.minute_data_import_export(days_to_fetch, self.now_utc, export_entity, scale=1.0, increment=False, smoothing=False)

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
                    import json

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
            return {}

        if not self.data_ready:
            self.log("ML Component: No load data available for prediction")
            return {}

        if not self.model_valid:
            self.log("ML Component: Model not valid ({}), returning empty predictions".format(self.model_status))
            return {}

        # Generate predictions using current model
        try:
            predictions = self.predictor.predict(self.load_data, now_utc, midnight_utc, pv_minutes=self.pv_data, temp_minutes=self.temperature_data, import_rates=self.import_rates_data, export_rates=self.export_rates_data, exog_features=exog_features)

            if predictions:
                self.current_predictions = predictions
                self.log("ML Component: Generated {} predictions (total {:.2f} kWh over 48h)".format(len(predictions), max(predictions.values()) if predictions else 0))

            return predictions

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

        if is_initial:
            # Initial run, need to fetch data and train model before we can provide predictions
            should_train = True
            should_fetch = True
        elif should_train:
            # Training requires fetching
            should_fetch = True

        if should_fetch:
            async with self.data_lock:
                load_data, age_days, load_minutes_now, pv_data, temperature_data, import_rates_data, export_rates_data = await self._fetch_load_data()
                if load_data:
                    self.load_data = load_data
                    self.load_data_age_days = age_days
                    self.load_minutes_now = load_minutes_now
                    self.pv_data = pv_data
                    self.temperature_data = temperature_data
                    self.import_rates_data = import_rates_data
                    self.export_rates_data = export_rates_data
                    self.last_data_fetch = self.now_utc
                    self.data_ready = True
                    if age_days > 3:
                        self.ml_validation_holdout_hours = 48
                    else:
                        self.ml_validation_holdout_hours = 24
                else:
                    self.log("Warn: ML Component: Failed to fetch load data, no data was returned.")

        # Check if we have data
        if not self.data_ready:
            if first:
                self.log("ML Component: Waiting for load data from sensors")
            return True  # Not an error, just waiting

        # Check if we have enough data
        if self.load_data_age_days < self.ml_min_days:
            self.model_status = "insufficient_data"
            self.model_valid = False
            if first:
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
            self._get_predictions(self.now_utc, self.midnight_utc)
            # Publish entity with current state
            self._publish_entity()
            self.log("ML Component: Prediction cycle completed")

        self.update_success_timestamp()
        return True

    async def _do_training(self, is_initial):
        """
        Perform model training.

        Args:
            is_initial: True for full training, False for fine-tuning
        """
        async with self.data_lock:
            if not self.load_data:
                self.log("Warn: ML Component: No data for training")
                return

            # Warn if limited data
            if self.load_data_age_days < 3:
                self.log("Warn: ML Component: Training with only {} days of data, recommend 3+ days for better accuracy".format(self.load_data_age_days))

            try:
                # Run training in executor to avoid blocking
                epochs = self.ml_epochs_initial if is_initial else self.ml_epochs_update

                val_mae = self.predictor.train(
                    self.load_data,
                    self.now_utc,
                    pv_minutes=self.pv_data,
                    temp_minutes=self.temperature_data,
                    import_rates=self.import_rates_data,
                    export_rates=self.export_rates_data,
                    is_initial=is_initial,
                    epochs=epochs,
                    time_decay_days=min(self.ml_time_decay_days, self.load_data_age_days),
                    validation_holdout_hours=self.ml_validation_holdout_hours,
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

                    # Save model
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
