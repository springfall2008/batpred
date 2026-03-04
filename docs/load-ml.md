# ML Load Prediction

Predbat includes a neural network-based machine learning component that can predict your household energy consumption for the next 48 hours.
This prediction is based on historical load patterns, time-of-day patterns, day-of-week patterns, and optionally PV generation history and temperature forecasts.

## Table of Contents

- [Overview](#overview)
- [How the Neural Network Works](#how-the-neural-network-works)
- [Configuration](#configuration)
- [History Accumulation and the Database](#history-accumulation-and-the-database)
- [Setup Instructions](#setup-instructions)
- [Understanding the Model](#understanding-the-model)
- [Monitoring and Troubleshooting](#monitoring-and-troubleshooting)
- [Model Persistence](#model-persistence)
- [Advanced Training Options](#advanced-training-options)

## Overview

The ML Load Prediction component uses a lightweight multi-layer perceptron (MLP) neural network implemented in pure NumPy. It learns from your historical energy consumption patterns and makes predictions about future load.

**Key Features:**

- Predicts 48 hours of load data in 5-minute intervals
- Learns daily and weekly patterns automatically
- Supports historical PV generation data as an input feature
- Supports temperature forecast data for improved accuracy
- Uses historical and future energy import/export rates as input features
- Deep neural network with 3 hidden layers [512, 256, 64 neurons]
- Optimized with He initialization and AdamW weight decay for robust training
- Automatically trains on historical data (requires at least 1 day, recommended 7+ days; fetches up to `load_ml_max_days_history` days from HA and accumulates up to `load_ml_database_days` days in the on-disk database)
- Fine-tunes periodically (every 2 hours) using full dataset to adapt to changing patterns
- Time-weighted training prioritizes recent data while learning from historical patterns
- Model persists across restarts
- Falls back gracefully if predictions are unreliable

## How the Neural Network Works

### Architecture

The ML Load Predictor uses a deep multi-layer perceptron (MLP) with the following architecture:

- **Input Layer**: 1446 features (288 load + 288 PV + 288 temperature + 288 import rates + 288 export rates + 6 time features)
- **Hidden Layers**: 3 layers with [512, 256, 64] neurons using ReLU activation
- **Output Layer**: 1 neuron (predicts next 5-minute step)
- **Total Parameters**: ~889,000 trainable weights

**Optimization Techniques:**

- **He Initialization**: Weights initialized using He/Kaiming method (`std = sqrt(2/fan_in)`), optimized for ReLU activations
- **AdamW Optimizer**: Adam optimization with weight decay (L2 regularization, default 0.01) to prevent overfitting
- **Cosine LR Decay**: Learning rate decays from `lr_max` (0.001) to `lr_min` (0.0001) following a cosine curve over all epochs, reducing oscillation in late training
- **Huber Loss**: Training uses Huber loss (δ=1.35 in normalised space) instead of MSE, which reduces the influence of individual spike events (e.g. EV charging) on the gradient
- **Inverted Dropout**: Random neurons are dropped during training (default rate 0.1) to reduce overfitting; no scaling is needed at inference time
- **Early Stopping**: Training halts when the EMA-smoothed combined metric `val_mae * 0.5 + |val_bias_median|` stops improving. The EMA (α=0.3) smooths out epoch-to-epoch noise caused by stochastic mini-batch sampling. Median bias (rather than mean) is used so a single outlier step cannot prematurely trigger a checkpoint
- **Weighted Samples**: Recent data weighted more heavily (exponential decay over history period)
- **Curriculum Learning**: Initial training begins with the oldest available data and progressively expands the window, so the model builds up general patterns before seeing the full history

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

5. **Cyclical Time Features** (6 features)
   - Sin/Cos encoding of minute-of-day (captures daily patterns with 5-min precision)
   - Sin/Cos encoding of day-of-week (captures weekly patterns)
   - Sin/Cos encoding of day-of-year (captures seasonality — winter heating vs summer load profiles)
   - These features help the network understand that 23:55 is close to 00:05, that Sunday is close to Monday, and that December is close to January

### Prediction Process

The model uses an autoregressive approach:

1. Takes the last 24 hours of historical data
2. Predicts the next 5-minute step
3. Adds that prediction to the history window
4. Shifts the window forward and repeats
5. Continues for 576 steps to cover 48 hours

To prevent drift in long-range predictions, the model blends autoregressive predictions with historical daily patterns. The blending uses **day-of-week-aware patterns**: separate average profiles are maintained for each of the 7 days of the week (Monday–Sunday), so the weekend fallback differs from weekday. If a particular day of the week has insufficient data (fewer than 2 complete observations per slot), the global all-days average is used instead.

### Training Process

**Initial Training:**

- Requires at least 1 day of historical data (7+ days recommended, configurable via `load_ml_max_days_history`)
- Fetches up to `load_ml_max_days_history` days (default: 28) from HA, then merges with accumulated database history (up to `load_ml_database_days`, default: 90 days)
- Uses 100 epochs with early stopping (patience=5)
- Batch size: 128 samples
- AdamW optimizer with learning rate 0.001 and weight decay 0.01
- Sample weighting: exponential time decay (recent data weighted more)
- Validates on the last 24 hours of data
- Saves model to disk: `predbat_ml_model.npz`

**Regularization:**

- **Weight Decay**: L2 penalty (0.01) applied to network weights to prevent overfitting
- **Dropout**: 10% of hidden neurons are randomly dropped during each training forward pass (inverted dropout — no scaling needed at inference). Reduces over-reliance on any single neuron.
- **Huber Loss**: The training loss function transitions from quadratic (for small errors) to linear (for large errors) at a threshold of δ=1.35 in normalised space. This makes gradient updates robust to spike events such as EV charging or tumble-dryer loads without requiring them to be filtered out of training data.
- **Early Stopping**: Training halts when the EMA-smoothed combined metric `val_mae + 0.5 × |val_bias_median|` stops improving for more than `patience` consecutive epochs, selecting the best checkpoint seen so far. The EMA smoothing (α=0.3) prevents a single noisy epoch from triggering an early stop or a premature checkpoint. Median bias is used so that a single outlier sample in the validation set cannot dominate the stopping decision.
- **Time-Weighted Samples**: Recent data has higher importance (7-day exponential decay constant)
    - Today's data: 100% weight
    - N days old: 37% weight (e^-1)

### Curriculum Training (Initial Training Only)

When the model is trained for the first time on a new or reset dataset, Predbat uses **curriculum learning** rather than a single pass over all available data.

**How it works:**

1. The available history is divided into progressively larger windows starting from the oldest data.
2. The first pass trains on only the oldest `ml_curriculum_window_days` days (default 7 days), using the most recent 24 h of *that slice* as the validation holdout.
3. Each subsequent pass expands the training window by `ml_curriculum_step_days` days (default 1 day), again validating on the last 24 h of the slice.
4. After at most `ml_curriculum_max_passes` intermediate passes (default 4), the final pass trains on the complete dataset with the standard holdout window.

**Why curriculum training?**

Training directly on months of mixed data can make it hard for the network to spot long-run weekly cycles. By starting small and expanding, the model:

- First learns the simplest daily patterns from old data
- Progressively refines those patterns as newer data is added
- Arrives at the final full-data pass with a much better initialisation than random weights would provide

**Fine-tuning:**

- Runs once the model age reaches the retrain interval (default 2 hours) rather than on a fixed clock tick, so restarts do not reset the interval unnecessarily
- Uses the full dataset (not curriculum) since the model is already well-initialised
- Fetches from HA and merges with accumulated database history (up to `load_ml_database_days` total)
- Uses 30 epochs with early stopping to quickly adapt to recent changes
- Applies same time-weighted sampling to prioritize recent data
- Preserves learned patterns while adapting to new ones
- Same regularization techniques applied as initial training
- Each fine-tune cycle blends the current data's feature statistics (mean/std) with the stored normalization parameters via an exponential moving average (alpha=0.1).
This lets the model slowly track long-term shifts in feature distributions (e.g. seasonal load changes, new tariff rates) without sudden jumps that could destabilise existing weights.

**Why Full Dataset for Fine-tuning?**

Although fine-tuning uses up to 20 epochs (vs 100 for initial training), it still uses the full dataset with time-weighted sampling. This approach:

- **Prevents catastrophic forgetting**: Using only recent data would cause the model to gradually forget older patterns
- **Balances adaptation**: Time weighting ensures recent changes are prioritized while maintaining long-term pattern knowledge
- **Handles seasonal patterns**: A deep history database (default 90 days) helps capture weekly cycles and seasonal trends
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

  # Optional: Maximum days of historical data to fetch from HA on each poll (default: 28)
  # load_ml_max_days_history: 28

  # Optional: Number of days of history to accumulate in the on-disk database (default: 90)
  # load_ml_database_days: 90
```

**Configuration Parameter Details:**

- `load_ml_enable`: Enables the ML component (required)
- `load_ml_source`: When `true`, Predbat uses ML predictions for battery planning. Set to `false` to test predictions without affecting battery control
- `load_ml_max_days_history`: Maximum days of historical data to fetch from Home Assistant on each poll (every 30 minutes)
    - **Default**: 28 days
    - **Minimum**: 7 days
    - **Recommended**: 28 days
    - **Constraint**: Limited by your HA recorder retention period — you cannot fetch more history than HA has stored
    - **When to increase**: If you have very regular weekly patterns or want seasonal awareness
    - **When to decrease**: If your consumption patterns change frequently, or you have limited historical data storage
    - **Note**: Training time increases slightly with more data, but fine-tuning remains fast due to importance-weighted sampling
- `load_ml_database_days`: Number of days of history to accumulate and persist in the on-disk database file (`predbat_ml_history.npz`)
    - **Default**: 90 days
    - **How it works**: See [History Accumulation and the Database](#history-accumulation-and-the-database) below
    - **When to increase**: If you want the model to learn long-term seasonal patterns (e.g. summer vs winter)
    - **When to decrease**: To save disk space, or if you prefer the model to forget older patterns faster
    - **Disk usage**: Each day of history uses approximately 5.6 KB (5 channels × 288 steps/day × 4 bytes, plus minimal metadata/format overhead)

### History Accumulation and the Database

The ML component maintains two distinct layers of historical data:

**Live fetch layer** (`load_ml_max_days_history`):
Every 30 minutes the component fetches the most recent N days of sensor history from Home Assistant. This is limited by your HA recorder retention — if HA only stores 14 days then that is all you will get regardless of what `load_ml_max_days_history` is set to.

**Accumulated database layer** (`load_ml_database_days`):
After each successful fetch, the newly fetched data is *merged* with the existing in-memory dataset and saved to `predbat_ml_history.npz`. This means history accumulates over time, well beyond what a single HA fetch can provide. For example with a 14-day HA retention and `load_ml_database_days: 90` set, after 90 days of running the model will have 90 days of load history to train on — far more than HA alone could supply.

**How the merge works:**
Before each fetch the existing in-memory data is time-shifted forward so that all keys remain anchored to "minutes ago from now". Fresh data from HA is then merged on top, with the fresh values taking priority for the most recent period. Older keys that have shifted beyond `load_ml_database_days` are dropped.

This means:

- `load_ml_max_days_history` controls how much fresh data is pulled from HA each cycle (bounded by HA retention)
- `load_ml_database_days` controls the total depth of the training dataset that accumulates on disk
- Setting `load_ml_database_days` to 0 or leaving `load_ml_database_days` unset disables the database entirely — training only ever uses what HA currently has
- The age reported in logs and the `training_days` attribute reflects the actual depth of the accumulated dataset, computed from the furthest key present in memory

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
4. Ensure you have at least 1 day of historical data (7+ days recommended); the database will accumulate history over time beyond what HA retains

### Step 2: Enable the Component

Add `load_ml_enable: true` to your `apps.yaml` and restart Predbat.

### Step 3: Wait for Initial Training

On startup the component deliberately **defers initial training to the second run cycle**. This design keeps startup fast and avoids running a CPU-heavy training pass before the event loop is fully settled.

What happens on each cycle:

1. **Startup cycle** — load the history database (if present), fetch fresh data from HA, then return. No training yet.
2. **Second cycle (≈ 5 minutes later)** — training fires for the first time because the model has never been trained (`last_train_time` is unset).

The full initial training sequence is:

1. Load the history database and merge with a fresh HA fetch
2. Run curriculum training (progressive window expansion — see [Curriculum Training](#curriculum-training-initial-training-only))
3. Validate the model
4. Begin making predictions if validation passes

Check the Predbat logs for training progress:

```text
ML Component: Starting initial training
ML Predictor: Curriculum training - 4 passes, window 7.0→10.0 days + final full pass (full period)
ML Predictor: Training complete, final val_mae=0.0051 kWh val_bias=+0.0010 kWh (+2.0%)
ML Predictor: AR rollout over holdout: ar_mae=0.0135 kWh ar_bias=-0.0030 kWh (-6.2%) [drift vs teacher-forced: +0.0031 kWh]
ML Component: Initial training completed, validation MAE=0.0051 kWh
```

The **AR rollout** line shows how the model performs when predictions feed back into subsequent steps (autoregressive mode, as used in real predictions), compared to the teacher-forced validation MAE. A small drift (< 3× teacher-forced MAE) indicates the model handles its own outputs well.

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

1. **Sufficient Historical Data**: At least 7 days recommended for stable patterns; training uses the full accumulated database (up to `load_ml_database_days`, default 90 days) merged with recent HA history (up to `load_ml_max_days_history`, default 28 days)
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

### Tracking Normalization Drift

Each time the model trains (initial fit) or fine-tunes (EMA update), it logs a normalization stats line that summarises the mean and standard deviation for each input feature group.
You can search for Normalization stats in the logfile for this information.

Large shifts in `mean` or `std` for a group (e.g. `import_rate` after a tariff change, or `load` after a new appliance) will be visible here and confirm the EMA is tracking the drift correctly.

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

Two files are saved to your Predbat config directory:

### `predbat_ml_model.npz` — the trained neural network

This file contains:

- **Network weights and biases**: All 3 hidden layers plus output layer
- **Optimizer state**: Adam momentum terms for continuing fine-tuning
- **Normalization parameters**: Feature and target mean/standard deviation (updated via EMA each fine-tune cycle to track distribution drift)
- **Training metadata**: Epochs trained, timestamp, model version, architecture details

The model is automatically loaded on Predbat restart, allowing predictions to continue immediately without retraining. The EMA-updated normalization parameters are saved and restored with the model, so drift tracking is preserved across restarts.

**Note**: If you update Predbat and the model architecture or version changes, the old model will be rejected and a new model will be trained from scratch. If the model becomes unstable, you can manually delete `predbat_ml_model.npz` to force retraining.

### `predbat_ml_history.npz` — the accumulated history database

This file is created when `load_ml_database_days` is set (default: 90). It contains:

- **Five data channels**: load, PV, temperature, import rates, export rates — each stored as a dense float32 array covering `load_ml_database_days` days of 5-minute steps
- **Metadata**: Save timestamp, schema version, step size, and data age — used on reload to correctly time-shift the stored data before merging with fresh HA data

On startup the history database is loaded, time-shifted to align with the current time, then merged with a fresh fetch from HA. This means the training dataset immediately benefits from all accumulated history even before any new data arrives.

Future PV forecast values (negative keys) are never persisted — they are always re-fetched fresh.

**Resetting the database**: Delete `predbat_ml_history.npz` to discard all accumulated history and start fresh. The model file is independent and does not need to be deleted at the same time.

---

## Advanced Training Options

The following internal parameters are set in `load_ml_component.py` and are not currently exposed as `apps.yaml` keys, but are documented here for reference. They can be changed by editing the component directly if needed.

| Parameter | Default | Description |
|---|---|---|
| `ml_curriculum_window_days` | 7 | Size (days) of the initial training window in the first curriculum pass |
| `ml_curriculum_step_days` | 1 | Days added to the training window for each subsequent curriculum pass |
| `ml_curriculum_max_passes` | 4 | Maximum number of intermediate curriculum passes before the final full-data pass; `0` means unlimited |
| `ml_dropout_rate` | 0.1 | Fraction of hidden neurons randomly dropped during training to reduce over-fitting |
| `ml_weight_decay` | 0.01 | L2 regularization coefficient for AdamW (larger = more regularization) |
| `ml_learning_rate` | 0.001 | Adam optimizer learning rate |
| `ml_epochs_initial` | 100 | Max epochs for initial (full) training; early stopping usually fires first |
| `ml_epochs_update` | 30 | Max epochs for each fine-tune cycle |
| `ml_patience_initial` | 10 | Early-stopping patience (epochs) for initial training |
| `ml_patience_update` | 10 | Early-stopping patience (epochs) for fine-tuning |
| `ml_validation_threshold` | 2.0 | Maximum allowable validation MAE (kWh) before predictions are disabled |
| `ml_max_model_age_hours` | 48 | Hours after which a model is considered stale and requires retraining |
| `ml_time_decay_days` | 30 | Exponential time-decay constant for sample weighting (older samples get lower weight) |
| `ml_validation_holdout_hours` | 24 | Hours of most-recent data held out for validation (not used in training) |
| `ml_huber_delta` | 1.35 | Huber loss transition point in normalised target units; errors below this are penalised quadratically, above it linearly. Lower values make training more robust to spikes; higher values bring it closer to MSE |
| `ml_ema_smoothing_alpha` | 0.3 | EMA alpha applied to the early-stopping metric across epochs (0 = no smoothing, 1 = no memory). Higher values react faster to per-epoch changes but are noisier |
| `ml_lr_decay` | `"cosine"` | Learning rate schedule: `"cosine"` decays from `ml_learning_rate` to 10% of it over all epochs; `None` keeps it constant |

---

## See Also

- [Components Documentation](components.md) - Overview of all Predbat components
- [Configuration Guide](configuration-guide.md) - General configuration guidance
- [Temperature Component](components.md#temperature-api-temperature) - Setup guide for temperature forecasts
- [Customisation Guide](customisation.md) - Advanced customisation options
