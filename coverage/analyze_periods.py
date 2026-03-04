#!/usr/bin/env python3
import json

# Load the data
with open("load_minutes_debug.json", "r") as f:
    load_data = {int(k): float(v) for k, v in json.load(f).items()}

# Convert to energy per step
STEP_MINUTES = 5
energy_per_step = {}
sorted_minutes = sorted(load_data.keys())

for minute in sorted_minutes:
    if minute + STEP_MINUTES in load_data:
        energy = max(0, load_data[minute] - load_data[minute + STEP_MINUTES])
        energy_per_step[minute] = energy

# Analyze different time periods
periods = [("Recent (0-1440min, 0-24h)", 0, 1440), ("Recent (0-2880min, 0-48h)", 0, 2880), ("Training window (2880-10080min, 2-7 days ago)", 2880, 10080), ("Full dataset", 0, max(energy_per_step.keys()))]

for name, start, end in periods:
    values = [energy_per_step[m] for m in energy_per_step.keys() if start <= m < end]
    if values:
        mean_val = sum(values) / len(values)
        max_val = max(values)
        median_val = sorted(values)[len(values) // 2]
        print(f"{name}:")
        print(f"  Count: {len(values)}, Mean: {mean_val:.4f} kWh, Median: {median_val:.4f} kWh, Max: {max_val:.4f} kWh")
    else:
        print(f"{name}: No data")
