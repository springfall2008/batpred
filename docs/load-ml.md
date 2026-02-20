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
- Uses historical and future energy import/export rates as input features
- Deep neural network with 4 hidden layers [512, 256, 128, 64 neurons]
- Optimized with He initialization and AdamW weight decay for robust training
- Automatically trains on historical data (requires at least 1 day, recommended 7+ days, up to 28 days configurable)
- Fine-tunes periodically (every 2 hours) using full dataset to adapt to changing patterns
- Time-weighted training prioritizes recent data while learning from historical patterns
- Model persists across restarts
- Falls back gracefully if predictions are unreliable

## How the Neural Network Works

### Architecture

The ML Load Predictor uses a deep multi-layer perceptron (MLP) with the following architecture:

- **Input Layer**: 1444 features (288 load + 288 PV + 288 temperature + 288 import rates + 288 export rates + 4 time features)
- **Hidden Layers**: 4 layers with [512, 256, 128, 64] neurons using ReLU activation
- **Output Layer**: 1 neuron (predicts next 5-minute step)
- **Total Parameters**: ~500,000 trainable weights

**Optimization Techniques:**

- **He Initialization**: Weights initialized using He/Kaiming method (`std = sqrt(2/fan_in)`), optimized for ReLU activations
- **AdamW Optimizer**: Adam optimization with weight decay (L2 regularization, default 0.01) to prevent overfitting
- **Early Stopping**: Training halts if validation error stops improving (patience=5 epochs)
- **Weighted Samples**: Recent data weighted more heavily (exponential decay over history period)

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
   - Past 24 hours of temperature data at 5-minute intervals
   - Helps correlate temperature with energy usage (heating/cooling)
   - **Requires the Temperature component to be enabled**

4. **Historical Import/Export Energy Rates** (288 + 288 features)
   - Past 24 hours of electricity import rates at 5-minute intervals
   - Past 24 hours of electricity export rates at 5-minute intervals
   - Helps the model learn consumption patterns based on time-of-use pricing
   - Automatically extracted from your configured Octopus Energy tariffs or other rate sources
   - Particularly useful for homes that shift usage to cheaper rate periods

5. **Cyclical Time Features** (4 features)
   - Sin/Cos encoding of minute-of-day (captures daily patterns with 5-min precision)
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

- Requires at least 1 day of historical data (7+ days recommended, up to 28 days configurable)
- Fetches up to 28 days of load history by default (configurable via `load_ml_max_days_history`)
- Uses 100 epochs with early stopping (patience=5)
- Batch size: 128 samples
- AdamW optimizer with learning rate 0.001 and weight decay 0.01
- Sample weighting: exponential time decay over 7 days (recent data weighted more)
- Validates on the last 24 hours of data
- Saves model to disk: `predbat_ml_model.npz`

**Regularization:**

- **Weight Decay**: L2 penalty (0.01) applied to network weights to prevent overfitting
- **Early Stopping**: Training halts if validation error doesn't improve for 5 consecutive epochs
- **Time-Weighted Samples**: Recent data has higher importance (7-day exponential decay constant)
    - Today's data: 100% weight
    - N days old: 37% weight (e^-1)

**Fine-tuning:**

- Runs every 2 hours if enabled
- Uses full available dataset (same as initial training, up to 28 days)
- Uses 3 epochs to quickly adapt to recent changes
- Applies same time-weighted sampling to prioritize recent data
- Preserves learned patterns while adapting to new ones
- Same regularization techniques applied as initial training

**Why Full Dataset for Fine-tuning?**

Although fine-tuning uses up to 20 epochs (vs 100 for initial training), it still uses the full dataset with time-weighted sampling. This approach:

- **Prevents catastrophic forgetting**: Using only recent data would cause the model to gradually forget older patterns
- **Balances adaptation**: Time weighting ensures recent changes are prioritized while maintaining long-term pattern knowledge
- **Handles seasonal patterns**: 28 days of history helps capture weekly cycles and early seasonal trends
- **Provides stability**: The model learns from a broader context, making predictions more robust

With time-weighted sampling, training samples have these relative weights:

- **This week**: 100% - 50% (fully weighted)
- **Last week**: 50% - 20% (moderately weighted)
- **2 weeks ago**: 20% - 7% (lightly weighted)
- **3-4 weeks ago**: 7% - 1% (minimal weight, but still contributing to pattern learning)

This allows the model to adapt quickly to recent changes (via time weighting) without losing the benefit of learning from historical patterns.

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

  # Optional: Maximum days of historical data to use for training (default: 28)
  # load_ml_max_days_history: 28
```

**Configuration Parameter Details:**

- `load_ml_enable`: Enables the ML component (required)
- `load_ml_source`: When `true`, Predbat uses ML predictions for battery planning. Set to `false` to test predictions without affecting battery control
- `load_ml_max_days_history`: Maximum days of historical data to fetch and train on
    - **Default**: 28 days
    - **Minimum**: 1 day (not recommended for production)
    - **Recommended**: 7-28 days depending on your consumption patterns
    - **When to increase**: If you have very regular weekly patterns or want seasonal awareness
    - **When to decrease**: If your consumption patterns change frequently, or you have limited historical data storage
    - **Note**: Training time increases slightly with more data, but fine-tuning remains fast (3 epochs)

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
4. Ensure you have at least 1 day of historical data (7+ days recommended, up to 28 days by default)

### Step 2: Enable the Component

Add `load_ml_enable: true` to your `apps.yaml` and restart Predbat.

### Step 3: Wait for Initial Training

On first run, the component will:

1. Fetch historical load data (default: up to 28 days, configurable)
2. Train the neural network (takes 1-5 minutes depending on data)
3. Validate the model
4. Begin making predictions if validation passes

Check the Predbat logs for training progress:

```text
ML Component: Starting initial training
ML Predictor: Starting initial training with 100 epochs
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
- **Validation MAE**: Mean Absolute Error on validation data (see [Understanding MAE](#understanding-mae-mean-absolute-error) for details)
- **Model Age**: How long since the model was last trained

You can check model status in the Predbat logs or via the component status page in the web interface.

### What Makes Good Predictions?

Good predictions require:

1. **Sufficient Historical Data**: At least 7 days recommended for stable patterns (supports up to 28 days by default)
2. **Consistent Patterns**: Regular daily/weekly routines improve accuracy
3. **Temperature Data**: Especially important for homes with electric heating/cooling (requires Temperature component)
4. **Energy Rate Data**: Automatically included - helps model learn consumption patterns based on time-of-use tariffs
5. **PV Generation Data**: If you have solar panels, include `pv_today` sensor for better correlation
6. **Clean Data**: Avoid gaps or incorrect readings in historical data
7. **Recent Training**: Model retrains every 2 hours using full dataset with time-weighted sampling to adapt to changing patterns

### Understanding MAE (Mean Absolute Error)

The model's accuracy is measured using **MAE (Mean Absolute Error)**, which is the primary metric used for validation and monitoring.

**What is MAE?**

MAE measures the average absolute difference between predicted and actual energy consumption values. For example:

- If the model predicts 0.5 kWh for a 5-minute period and actual consumption is 0.7 kWh, the error is 0.2 kWh
- MAE is the average of these errors across all predictions

**How to interpret MAE:**

- **MAE is in kWh per 5-minute step** - this is the average prediction error for each 5-minute interval
- **Lower is better** - an MAE of 0.3 kWh means predictions are typically off by ±0.3 kWh per 5-minute period
- **Scale matters** - a 0.3 kWh error means different things for different households:
    - Low consumption home (2 kW average): 0.3 kWh per 5-min ≈ 3.6 kW error → significant
    - High consumption home (8 kW average): 0.3 kWh per 5-min ≈ 3.6 kW error → moderate

**Practical example:**

If your validation MAE is 0.4 kWh per 5-min step:

- Each 5-minute prediction is off by an average of 0.4 kWh (±24 Wh/min)
- This translates to roughly ±4.8 kW average power error
- Over 1 hour (12 steps), cumulative error averages out but could be up to ±4.8 kWh
- The model learns patterns, so errors tend to cancel out over longer periods

**Why MAE is used:**

- **Easy to interpret**: Errors are in the same units as predictions (kWh)
- **Robust to outliers**: Unlike squared errors, large mistakes don't dominate the metric
- **Practical measure**: Directly relates to how much your battery plan might be affected

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

- **Network weights and biases**: All 4 hidden layers plus output layer
- **Optimizer state**: Adam momentum terms for continuing fine-tuning
- **Normalization parameters**: Feature and target mean/standard deviation
- **Training metadata**: Epochs trained, timestamp, model version, architecture details

The model is automatically loaded on Predbat restart, allowing predictions to continue immediately without retraining.

**Note**: If you update Predbat and the model architecture or version changes, the old model will be rejected and a new model will be trained from scratch. If the model becomes unstable, you can manually delete `predbat_ml_model.npz` to force retraining.

---

## See Also

- [Components Documentation](components.md) - Overview of all Predbat components
- [Configuration Guide](configuration-guide.md) - General configuration guidance
- [Temperature Component](components.md#temperature-api-temperature) - Setup guide for temperature forecasts
- [Customisation Guide](customisation.md) - Advanced customisation options
