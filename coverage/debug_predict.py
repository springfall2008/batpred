#!/usr/bin/env python3
"""Debug the prediction issue"""
import sys

sys.path.insert(0, "../apps/predbat")

import json
import numpy as np
from datetime import datetime, timezone, timedelta
from load_predictor import LoadPredictor, LOOKBACK_STEPS, STEP_MINUTES

# Load data
with open("load_minutes_debug.json", "r") as f:
    load_data = {int(k): float(v) for k, v in json.load(f).items()}

# Quick mode - just check final energies
if len(sys.argv) > 1 and sys.argv[1] == "--quick":
    predictor = LoadPredictor(learning_rate=0.001, max_load_kw=20.0)
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    predictor.train(load_data, now_utc, is_initial=True, epochs=30, time_decay_days=7)
    predictions = predictor.predict(load_data, now_utc, midnight_utc)

    pred_keys = sorted(predictions.keys())
    energies = []
    for i, minute in enumerate(pred_keys):
        if i == 0:
            energies.append(predictions[minute])
        else:
            energies.append(predictions[minute] - predictions[pred_keys[i - 1]])

    print("Energy stats:")
    print(f"  Min: {min(energies):.4f}, Max: {max(energies):.4f}, Mean: {np.mean(energies):.4f}")
    print(f"  Steps 0-20: {[round(e, 4) for e in energies[0:20]]}")
    print(f"  Steps 200-220: {[round(e, 4) for e in energies[200:220]]}")
    print(f"  Steps 400-420: {[round(e, 4) for e in energies[400:420]]}")
    print(f"  Steps 550-576: {[round(e, 4) for e in energies[550:576]]}")
    sys.exit(0)

# Train model
predictor = LoadPredictor(learning_rate=0.001, max_load_kw=20.0)
now_utc = datetime.now(timezone.utc)
midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

print("Training model...")
predictor.train(load_data, now_utc, is_initial=True, epochs=30, time_decay_days=7)

# Check normalization parameters
print(f"\n=== Normalization Parameters ===")
print(f"Feature mean (first 10 lookback): {predictor.feature_mean[:10]}")
print(f"Feature std (first 10 lookback): {predictor.feature_std[:10]}")
print(f"Target mean: {predictor.target_mean:.6f}")
print(f"Target std: {predictor.target_std:.6f}")

# Get the energy per step for historical data
energy_per_step = predictor._load_to_energy_per_step(load_data)

# Look at the initial lookback buffer
print(f"\n=== Initial Lookback Buffer ===")
lookback_buffer = []
for lb_offset in range(LOOKBACK_STEPS):
    lb_minute = lb_offset * STEP_MINUTES
    if lb_minute in energy_per_step:
        lookback_buffer.append(energy_per_step[lb_minute])
    else:
        lookback_buffer.append(0)

print(f"First 10 values: {lookback_buffer[:10]}")
print(f"Mean: {np.mean(lookback_buffer):.6f}, Std: {np.std(lookback_buffer):.6f}")
print(f"Min: {np.min(lookback_buffer):.6f}, Max: {np.max(lookback_buffer):.6f}")

# Now trace through a few prediction steps
print(f"\n=== Prediction Step-by-Step ===")
predictions_energy = []

for step_idx in range(200):  # First 200 steps (16+ hours)
    target_time = now_utc + timedelta(minutes=(step_idx + 1) * STEP_MINUTES)
    minute_of_day = target_time.hour * 60 + target_time.minute
    day_of_week = target_time.weekday()
    time_features = predictor._create_time_features(minute_of_day, day_of_week)

    # Combine features
    features = np.concatenate([np.array(lookback_buffer, dtype=np.float32), time_features])

    # Normalize
    features_norm = predictor._normalize_features(features.reshape(1, -1), fit=False)

    # Forward pass
    pred_norm, _, _ = predictor._forward(features_norm)

    # Denormalize
    pred_energy = predictor._denormalize_predictions(pred_norm[0])

    # Clip
    pred_clipped = predictor._clip_predictions(pred_energy)
    energy_value = float(pred_clipped[0])

    print(f"Step {step_idx}: lb_mean={np.mean(lookback_buffer):.4f}, " f"pred_norm={pred_norm[0][0]:.4f}, pred_denorm={pred_energy[0]:.4f}, " f"pred_clipped={energy_value:.4f}")

    predictions_energy.append(energy_value)

    # Update lookback buffer
    lookback_buffer.insert(0, energy_value)
    lookback_buffer.pop()

# Check for the issue - when does it first go to zero?
print(f"\n=== Full Prediction Analysis ===")
full_predictions = predictor.predict(load_data, now_utc, midnight_utc)

# Show cumulative values
pred_keys = sorted(full_predictions.keys())
print("\nFirst 20 cumulative values:")
for i in range(20):
    print(f"  minute {pred_keys[i]}: {full_predictions[pred_keys[i]]:.4f}")

print("\nAround step 120-140:")
for i in range(120, 140):
    print(f"  minute {pred_keys[i]}: {full_predictions[pred_keys[i]]:.4f}")

# Convert to energy
pred_energy_list = []
sorted_minutes = sorted(full_predictions.keys())
prev_cum = 0
for minute in sorted_minutes:
    cum = full_predictions[minute]
    energy = cum - prev_cum
    pred_energy_list.append(energy)
    prev_cum = cum

print(f"\nPrediction minutes: {sorted_minutes[:10]}...{sorted_minutes[-3:]}")
print(f"First 20 energies: {[f'{e:.4f}' for e in pred_energy_list[:20]]}")
print(f"Middle energies (140-160): {[f'{e:.4f}' for e in pred_energy_list[140:160]]}")
print(f"Late energies (200-220): {[f'{e:.4f}' for e in pred_energy_list[200:220]]}")

# Check for zeros or near-zeros
zeros = [(i, e) for i, e in enumerate(pred_energy_list) if e < 0.01]
print(f"\nSteps with energy < 0.01: {len(zeros)}")
if zeros:
    print(f"First 10: {zeros[:10]}")

# Stats
print(f"\nOverall stats:")
print(f"  Min: {min(pred_energy_list):.4f}")
print(f"  Max: {max(pred_energy_list):.4f}")
print(f"  Mean: {np.mean(pred_energy_list):.4f}")
print(f"  Std: {np.std(pred_energy_list):.4f}")
