# ML Load Prediction

Predbat includes a neural network-based machine learning component that can predict your household energy consumption for the next 48 hours.
This prediction is based on historical load patterns, time-of-day patterns, day-of-week patterns, and optionally PV generation history and temperature forecasts.

## Table of Contents

- [Overview](#overview)
- [How the Neural Network Works](#how-the-neural-network-works)
- [Configuration](#configuration)
- [Setup Instructions](#setup-instructions)
- [Understanding the Model](#understanding-the-model)
- [Monitoring and Troubleshooting](#monitoring-and-troubleshooting)
- [Model Persistence](#model-persistence)

## Overview

The ML Load Prediction component uses a lightweight multi-layer perceptron (MLP) neural network implemented in pure NumPy. It learns from your historical energy consumption patterns and makes predictions about future load.

**Key Features:**

- Predicts 48 hours of load data in 5-minute intervals
- Learns daily and weekly patterns automatically
- Supports historical PV generation data as an input feature
- Supports temperature forecast data for improved accuracy
- Automatically trains on historical data (requires at least 1 day, recommended 7+ days)
- Fine-tunes periodically to adapt to changing patterns
- Model persists across restarts
- Falls back gracefully if predictions are unreliable

## How the Neural Network Works

### Architecture

The ML Load Predictor uses a deep neural network with an input layer, some hidden layers and an output layer.

### Input Features

The neural network uses several types of input features to make predictions:

1. **Historical Load Lookback**
   - Past 24 hours of energy consumption at 5-minute intervals
   - Helps the network understand recent usage patterns

2. **Historical PV Generation**
   - Past 24 hours of solar PV generation at 5-minute intervals
   - Helps correlate solar production with consumption patterns
   - Requires `pv_today` sensor to be configured

3. **Historical Temperature**
   - Past 7 days and future 2 days of temperature data at 5-minute intervals
   - Helps correlate temperature with energy usage (heating/cooling)
   - **Requires the Temperature component to be enabled**

4. **Cyclical Time Features** (4 features)
   - Sin/Cos encoding of hour-of-day (captures daily patterns)
   - Sin/Cos encoding of day-of-week (captures weekly patterns)
   - These features help the network understand that 23:55 is close to 00:05

### Prediction Process

The model uses an autoregressive approach:

1. Takes the last 24 hours of historical data
2. Predicts the next 5-minute step
3. Adds that prediction to the history window
4. Shifts the window forward and repeats
5. Continues for 576 steps to cover 48 hours

To prevent drift in long-range predictions, the model blends autoregressive predictions with historical daily patterns.

### Training Process

**Initial Training:**

- Requires at least 1 day of historical data (7+ days recommended)
- Uses 50 epochs with early stopping
- Validates on the last 24 hours of data
- Saves model to disk: `predbat_ml_model.npz`

**Fine-tuning:**

- Runs every 2 hours if enabled
- Uses last 24 hours of data
- Uses 2 epochs to quickly adapt to recent changes
- Preserves learned patterns while adapting to new ones

**Model Validation:**

- Model is validated after each training session
- If validation error exceeds threshold (default 2.0 kWh MAE), predictions are disabled
- Model is considered stale after 48 hours and requires retraining

## Configuration

### Basic Setup

To enable ML load prediction, add to your `apps.yaml`:

```yaml
predbat:
  module: predbat
  class: PredBat

  # Enable ML load prediction
  load_ml_enable: True
  # Use the output data in Predbat (can be False to explore the use without using the data)
  load_ml_source: True
```

For best results:

- Ensure you have a least a weeks worth of data before enabling load_ml_source.
- Make sure you do not have PredAI enabled at the same time
- Disable in day adjustment (switch.predbat_calculate_inday_adjustment) as the AI model will do that for you.

### Recommended: Enable Temperature Predictions

**For best results, enable the Temperature component to provide temperature forecasts:**

```yaml
predbat:
  # ... other config ...

  # Enable temperature predictions (RECOMMENDED for ML load prediction)
  temperature_enable: true

  # Optional: specify coordinates (defaults to zone.home)
  # temperature_latitude: 51.5074
  # temperature_longitude: -0.1278
```

The temperature data significantly improves prediction accuracy for homes with heating/cooling systems, as energy consumption is often correlated with outside temperature.

### Optional: Add PV Generation Data

Your PV data will be picked from the pv_today setting in Predbat already

```yaml
predbat:
  # ... other config ...

  pv_today:
    - sensor.my_solar_generation_today
```

### Optional: Subtract Car Charging

If you have an EV charger set in Predbat then this will be subtracted from predictions.
If this is not set then the default EV charging threshold is used if car_charging_hold is True.

```yaml
predbat:
  # ... other config ...

  # Optional: subtract car charging from load history
  car_charging_energy:
    - sensor.my_ev_charger_energy
```

## Setup Instructions

### Step 1: Verify Prerequisites

Before enabling ML load prediction:

1. Ensure you have a `load_today` sensor that tracks cumulative daily energy consumption
2. Optionally configure `pv_today` if you have solar panels
3. **Recommended**: Enable the Temperature component (Temperature Component in components documentation)
4. Ensure you have at least 1 day of historical data (7+ days recommended)

### Step 2: Enable the Component

Add `load_ml_enable: true` to your `apps.yaml` and restart Predbat.

### Step 3: Wait for Initial Training

On first run, the component will:

1. Fetch historical load data (default: 7 days)
2. Train the neural network (takes 1-5 minutes depending on data)
3. Validate the model
4. Begin making predictions if validation passes

Check the Predbat logs for training progress:

```text
ML Component: Starting initial training
ML Predictor: Starting initial training with 50 epochs
ML Predictor: Training complete, final val_mae=0.3245 kWh
ML Component: Initial training completed, validation MAE=0.3245 kWh
```

### Step 4: Monitor Predictions

Once trained, the component publishes predictions to:

- `sensor.predbat_load_ml_forecast` - Contains 48-hour prediction in `results` attribute

You can visualize these predictions in the Predbat web interface or by creating charts in Home Assistant.

## Understanding the Model

### Model Status

The ML component tracks several status indicators:

- **Model Status**: `not_initialized`, `training`, `active`, `validation_failed`, `stale`
- **Validation MAE**: Mean Absolute Error on validation data (in kWh per 5-min step)
- **Model Age**: How long since the model was last trained

You can check model status in the Predbat logs or via the component status page in the web interface.

### What Makes Good Predictions?

Good predictions require:

1. **Sufficient Historical Data**: At least 7 days recommended
2. **Consistent Patterns**: Regular daily/weekly routines improve accuracy
3. **Temperature Data**: Especially important for homes with electric heating/cooling
4. **Clean Data**: Avoid gaps or incorrect readings in historical data
5. **Recent Training**: Model should be retrained periodically (happens automatically)

### Expected Accuracy

Typical validation MAE values:

- **Excellent**: < 0.3 kWh per 5-min step (~ 3.6 kW average)
- **Good**: 0.3 - 0.5 kWh per 5-min step
- **Fair**: 0.5 - 1.0 kWh per 5-min step
- **Poor**: > 1.0 kWh per 5-min step (may indicate issues)

If validation MAE exceeds the threshold (default 2.0 kWh), predictions are disabled and the model will attempt to retrain.

## Monitoring and Troubleshooting

### Charts

The Predbat WebUI has two charts associated with LoadML:

The LoadML chart shows the correlation between your actual load and the predictions by charting this against the prediction 1 hour in the future and 8 hours in the future.

<img width="1602" height="971" alt="Predbat LoadML chart showing actual household load and ML predictions 1 hour and 8 hours ahead" src="https://github.com/user-attachments/assets/731ef153-01e4-4ed1-bc5b-df1305d84f41" />

The LoadMLPower chart shows a similar view as power, but also plots PV production, predicted PV production and temperature predictions.

### Check Model Status

View model status in Predbat logs:

```text
ML Component: Model status: active, last trained: 2024-02-07 10:30:00
ML Component: Validation MAE: 0.3245 kWh
```

### Common Issues

**Issue**: Model never trains

- **Cause**: Insufficient historical data
- **Solution**: Wait until you have at least 1 day of data, preferably 7+ days

**Issue**: Validation MAE too high (predictions disabled)

- **Cause**: Inconsistent load patterns, poor data quality, or insufficient training data
- **Solution**:
    - Ensure historical data is accurate
    - Add temperature data if not already enabled
    - Wait for more historical data to accumulate
    - Check for gaps or anomalies in historical data

**Issue**: Model becomes stale

- **Cause**: No training for 48+ hours
- **Solution**: Check logs for training failures, ensure Predbat is running continuously

**Issue**: Predictions seem inaccurate

- **Cause**: Changing household patterns, insufficient features, or missing temperature data
- **Solution**:
    - Enable temperature predictions for better accuracy
    - Wait for fine-tuning to adapt to new patterns
    - Verify historical data quality
    - Consider adding PV data if you have solar panels

### Viewing Predictions

Access predictions via:

1. **Web Interface**: Navigate to the battery plan view to see ML predictions
2. **Home Assistant**: Check `sensor.predbat_load_ml_forecast` and its `results` attribute
3. **Logs**: Look for "ML Predictor: Generated predictions" messages

## Model Persistence

The trained model is saved to disk as `predbat_ml_model.npz` in your Predbat config directory. This file contains:

- Network weights and biases
- Normalization parameters (mean, standard deviation)
- Training metadata (epochs, timestamp, version)

The model is automatically loaded on Predbat restart, allowing predictions to continue immediately without retraining.

If the model becomes unstable you can also delete this file to start again.

---

## See Also

- [Components Documentation](components.md) - Overview of all Predbat components
- [Configuration Guide](configuration-guide.md) - General configuration guidance
- [Temperature Component](components.md#temperature-api-temperature) - Setup guide for temperature forecasts
- [Customisation Guide](customisation.md) - Advanced customisation options
