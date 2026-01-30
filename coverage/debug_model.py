#!/usr/bin/env python3
"""Debug script to analyze what the model is learning"""
import json
import sys
sys.path.insert(0, '../apps/predbat')
from load_predictor import LoadPredictor
from datetime import datetime, timezone

# Load data
with open('load_minutes_debug.json', 'r') as f:
    load_data = {int(k): float(v) for k, v in json.load(f).items()}

# Train model
predictor = LoadPredictor(learning_rate=0.001, max_load_kw=20.0)
now_utc = datetime.now(timezone.utc)

print("Training model...")
predictor.train(load_data, now_utc, is_initial=True, epochs=30, time_decay_days=7)

# Check normalization parameters
print(f"\nNormalization parameters:")
print(f"  Feature mean (first 12): {predictor.feature_mean[:12]}")  # Lookback values
print(f"  Feature mean (last 4): {predictor.feature_mean[12:]}")  # Time features
print(f"  Feature std (first 12): {predictor.feature_std[:12]}")
print(f"  Feature std (last 4): {predictor.feature_std[12:]}")
print(f"  Target mean: {predictor.target_mean:.4f} kWh")
print(f"  Target std: {predictor.target_std:.4f} kWh")

# Check first layer weights to see feature importance
print(f"\nFirst layer weight magnitudes (input importance):")
w1 = predictor.weights[0]  # Shape: (16, 32)
for i in range(16):
    mag = float((w1[i, :] ** 2).sum() ** 0.5)
    feat_name = f"lookback_{i}" if i < 12 else ["sin_minute", "cos_minute", "sin_day", "cos_day"][i-12]
    print(f"  {feat_name:15s}: {mag:.4f}")
