#!/usr/bin/env python3
import json
import statistics

# Load the data
with open("load_minutes_debug.json", "r") as f:
    load_data = {int(k): float(v) for k, v in json.load(f).items()}

# Convert to energy per step (like predictor does)
STEP_MINUTES = 5
energy_per_step = {}
sorted_minutes = sorted(load_data.keys())

for minute in sorted_minutes:
    if minute + STEP_MINUTES in load_data:
        energy = max(0, load_data[minute] - load_data[minute + STEP_MINUTES])
        energy_per_step[minute] = energy

# Get statistics
energies = list(energy_per_step.values())
print(f"Energy per step statistics:")
print(f"  Count: {len(energies)}")
print(f"  Min: {min(energies):.4f} kWh")
print(f"  Max: {max(energies):.4f} kWh")
print(f"  Mean: {statistics.mean(energies):.4f} kWh")
print(f"  Median: {statistics.median(energies):.4f} kWh")
print(f"  Std: {statistics.stdev(energies):.4f} kWh")
energies_sorted = sorted(energies)
print(f"  25th percentile: {energies_sorted[len(energies)//4]:.4f} kWh")
print(f"  75th percentile: {energies_sorted[3*len(energies)//4]:.4f} kWh")
print(f"  95th percentile: {energies_sorted[95*len(energies)//100]:.4f} kWh")

# Show first 24 hours of data
print(f"\nFirst 24 hours of data (minute 0-1440):")
for minute in range(0, min(1440, max(energy_per_step.keys())), 60):
    if minute in energy_per_step:
        print(f"  Minute {minute}: {energy_per_step[minute]:.4f} kWh")

# Check what the training data looks like
print(f"\nTraining window analysis (for predicting minute 0-2880):")
print(f"Looking at samples from minute 2880 onwards...")
for sample_minute in range(2880, min(2880 + 1440, max(energy_per_step.keys())), 60):
    if sample_minute in energy_per_step:
        print(f"  Sample at minute {sample_minute} (lookback from here): {energy_per_step[sample_minute]:.4f} kWh")
