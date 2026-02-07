# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# ML Load Forecaster Component - ComponentBase wrapper for LoadPredictor
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
import os
from datetime import datetime, timezone, timedelta
from component_base import ComponentBase
from utils import get_now_from_cumulative, dp2, minute_data
from load_predictor import LoadPredictor, MODEL_VERSION
from const import TIME_FORMAT, PREDICT_STEP

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

    def initialize(self, load_ml_enable, load_ml_source=True):
        """
        Initialize the ML load forecaster component.

        Args:
            load_ml_enable: Whether ML forecasting is enabled
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
        self.ml_epochs_initial = 50
        self.ml_epochs_update = 2
        self.ml_min_days = 1
        self.ml_validation_threshold = 2.0
        self.ml_time_decay_days = 7
        self.ml_max_load_kw = 50.0
        self.ml_max_model_age_hours = 48

        # Data state
        self.load_data = None
        self.load_data_age_days = 0
        self.pv_data = None
        self.temperature_data = None
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
        self.predictor = LoadPredictor(log_func=self.log, learning_rate=self.ml_learning_rate, max_load_kw=self.ml_max_load_kw)

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
                self.predictor = LoadPredictor(log_func=self.log, learning_rate=self.ml_learning_rate, max_load_kw=self.ml_max_load_kw)

    async def _fetch_load_data(self):
        """
        Fetch and process load data from configured sensors.

        Returns:
            Tuple of (load_minutes_dict, age_days, load_minutes_now, pv_data) or (None, 0, 0, None) on failure
        """
        if not self.ml_load_sensor:
            return None, 0, 0, None, None

        try:
            # Determine how many days of history to fetch, up to 7 days back
            days_to_fetch = max(7, self.ml_min_days)

            # Fetch load sensor history
            self.log("ML Component: Fetching {} days of load history from {}".format(days_to_fetch, self.ml_load_sensor))

            load_minutes, load_minutes_age = self.base.minute_data_load(self.now_utc, "load_today", days_to_fetch, required_unit="kWh", load_scaling=self.get_arg("load_scaling", 1.0), interpolate=True)
            if not load_minutes:
                self.log("Warn: ML Component: Failed to convert load history to minute data")
                return None, 0, 0, None, None

            if self.get_arg("load_power", default=None, indirect=False):
                load_power_data, _ = self.base.minute_data_load(self.now_utc, "load_power", days_to_fetch, required_unit="W", load_scaling=1.0, interpolate=True)
                load_minutes = self.base.fill_load_from_power(load_minutes, load_power_data)

            # Get current cumulative load value
            load_minutes_now = get_now_from_cumulative(load_minutes, self.minutes_now, backwards=True)

            car_charging_energy = {}
            if self.get_arg("car_charging_energy", default=None, indirect=False):
                car_charging_energy = self.base.minute_data_import_export(days_to_fetch, self.now_utc, "car_charging_energy", scale=self.car_charging_energy_scale, required_unit="kWh")

            max_minute = max(load_minutes.keys()) if load_minutes else 0
            max_minute = (max_minute // 5) * 5  # Align to 5-minute intervals
            load_minutes_new = {}

            # Subtract configured sensors (e.g., car charging)
            total_load_energy = 0
            car_delta = 0.0
            STEP = PREDICT_STEP
            for minute in range(max_minute, -STEP, -STEP):
                if self.car_charging_hold and car_charging_energy:
                    car_delta = abs(car_charging_energy.get(minute, 0.0) - car_charging_energy.get(minute - STEP, car_charging_energy.get(minute, 0.0)))
                elif self.car_charging_hold:
                    load_now = abs(load_minutes.get(minute, 0.0) - load_minutes.get(minute - STEP, load_minutes.get(minute, 0.0)))
                    if load_now >= self.car_charging_threshold * STEP:
                        car_delta = self.car_charging_rate * STEP
                if car_delta > 0:
                    # When car is enable spread over 5 minutes due to alignment between car and house load data
                    load_delta = abs(load_minutes.get(minute, 0.0) - load_minutes.get(minute - STEP, load_minutes.get(minute, 0.0)))
                    load_delta = max(0.0, load_delta - car_delta)
                    for m in range(minute, minute - STEP, -1):
                        load_minutes_new[m] = total_load_energy + load_delta / STEP
                    total_load_energy += load_delta
                else:
                    # Otherwise just copy load data
                    for m in range(minute, minute - STEP, -1):
                        load_delta = abs(load_minutes.get(minute, 0.0) - load_minutes.get(minute - 1, load_minutes.get(minute, 0.0)))
                        load_minutes_new[m] = total_load_energy
                        total_load_energy += load_delta

            # Calculate age of data
            age_days = max_minute / (24 * 60)

            # PV Data
            if self.ml_pv_sensor:
                pv_data, _ = self.base.minute_data_load(self.now_utc, "pv_today", days_to_fetch, required_unit="kWh", load_scaling=1.0, interpolate=True)
            else:
                pv_data = {}

            # Temperature predictions
            temp_entity = "sensor." + self.prefix + "_temperature"
            temperature_info = self.get_state_wrapper(temp_entity, attribute="results")
            temperature_data = {}
            if isinstance(temperature_info, dict):
                data_array = []
                for key, value in temperature_info.items():
                    data_array.append({"state": value, "last_updated": key})

                # Load data from past and future predictions, base backwards around now_utc
                # We also get the last 7 days in the past to help the model learn the daily pattern
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

            self.log("ML Component: Fetched {} load data points, {:.1f} days of history".format(len(load_minutes_new), age_days))
            # with open("input_train_data.json", "w") as f:
            #    import json
            #    json.dump([load_minutes_new, age_days, load_minutes_now, pv_data, temperature_data], f, indent=2)
            return load_minutes_new, age_days, load_minutes_now, pv_data, temperature_data

        except Exception as e:
            self.log("Error: ML Component: Failed to fetch load data: {}".format(e))
            print("Error: ML Component: Failed to fetch load data: {}".format(e))
            import traceback

            self.log("Error: ML Component: {}".format(traceback.format_exc()))
            return None, 0, 0, None, None

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
            predictions = self.predictor.predict(self.load_data, now_utc, midnight_utc, pv_minutes=self.pv_data, temp_minutes=self.temperature_data, exog_features=exog_features)

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

        # Fetch fresh load data periodically (every 15 minutes)
        should_fetch = first or ((seconds % PREDICTION_INTERVAL_SECONDS) == 0)

        if should_fetch:
            async with self.data_lock:
                load_data, age_days, load_minutes_now, pv_data, temperature_data = await self._fetch_load_data()
                if load_data:
                    self.load_data = load_data
                    self.load_data_age_days = age_days
                    self.load_minutes_now = load_minutes_now
                    self.data_ready = True
                    self.last_data_fetch = self.now_utc
                    pv_data = pv_data
                    pv_forecast_minute, pv_forecast_minute10 = self.base.fetch_pv_forecast()
                    # PV Data has the historical PV data (minute is the number of minutes in the past)
                    # PV forecast has the predicted PV generation for the next 24 hours (minute is the number of minutes from midnight forward
                    # Combine the two into a new dict where negative minutes are in the future and positive in the past
                    self.pv_data = pv_data
                    current_value = pv_data.get(0, 0)
                    if pv_forecast_minute:
                        max_minute = max(pv_forecast_minute.keys()) + PREDICT_STEP
                        for minute in range(self.minutes_now + PREDICT_STEP, max_minute, PREDICT_STEP):
                            current_value += pv_forecast_minute.get(minute, current_value)
                            pv_data[-minute + self.minutes_now] = current_value
                    self.temperature_data = temperature_data
                else:
                    self.log("Warn: ML Component: Failed to fetch load data")

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

        # Determine if training is needed
        should_train = False
        is_initial = False

        if not self.initial_training_done:
            # First training
            should_train = True
            is_initial = True
            self.log("ML Component: Starting initial training")
        elif seconds % RETRAIN_INTERVAL_SECONDS == 0:
            # Periodic fine-tuning every 2 hours
            should_train = True
            is_initial = False
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

                val_mae = self.predictor.train(self.load_data, self.now_utc, pv_minutes=self.pv_data, temp_minutes=self.temperature_data, is_initial=is_initial, epochs=epochs, time_decay_days=self.ml_time_decay_days)

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
                import traceback

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
            for minute, value in self.current_predictions.items():
                timestamp = self.midnight_utc + timedelta(minutes=minute + self.minutes_now)
                timestamp_str = timestamp.strftime(TIME_FORMAT)
                # Reset at midnight
                if minute > 0 and ((minute + self.minutes_now) % (24 * 60) == 0):
                    reset_amount = value + self.load_minutes_now
                output_value = round(value - reset_amount + self.load_minutes_now, 4)
                results[timestamp_str] = output_value
                if minute == 0:
                    power_today_now = value / PREDICT_STEP * 60.0
                if minute == 60:
                    load_today_h1 = output_value
                    power_today_h1 = value / PREDICT_STEP * 60.0
                if minute == 60 * 8:
                    load_today_h8 = output_value
                    power_today_h8 = value / PREDICT_STEP * 60.0

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

    def last_updated_time(self):
        """Return last successful update time for component health check."""
        return self.last_success_timestamp

    def is_alive(self):
        """Check if component is alive and functioning."""
        if not self.ml_enable:
            return True

        if self.last_success_timestamp is None:
            return False

        age = datetime.now(timezone.utc) - self.last_success_timestamp
        return age < timedelta(minutes=10)
