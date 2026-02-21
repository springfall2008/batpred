#!/usr/bin/env python3
"""
Plot load_minutes, load_minutes_orig and car_charging_energy from input_train_data.json
with improved car charging subtraction algorithm
"""

import json
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import numpy as np

# Load the data
with open('input_train_data.json', 'r') as f:
    data = json.load(f)

# Extract the data
load_minutes = {int(k): float(v) for k, v in data['load_minutes'].items()}
load_minutes_orig = {int(k): float(v) for k, v in data.get('load_minutes_orig', {}).items()}
car_charging_energy = {int(k): float(v) for k, v in data.get('car_charging_energy', {}).items()}


def improved_car_subtraction(load_cumulative, car_cumulative, step=5, 
                             interpolate_gaps=True, max_gap_minutes=60,
                             smoothing_window=3, debug=False):
    """
    Improved car charging subtraction that handles timing misalignment and gaps.
    
    Args:
        load_cumulative: Dict of {minute: cumulative_kwh} for load (backwards format)
        car_cumulative: Dict of {minute: cumulative_kwh} for car charging (backwards format)
        step: Step size in minutes (default 5)
        interpolate_gaps: Whether to interpolate missing car data
        max_gap_minutes: Maximum gap to interpolate (minutes)
        smoothing_window: Number of steps for smoothing car data
        debug: Print debug information
        
    Returns:
        Dict of {minute: cumulative_kwh} with improved car subtraction
    """
    if not car_cumulative or not load_cumulative:
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
    
    for minute in range(0, max_minute + step, step):
        next_minute = minute + step
        
        # Calculate load delta
        if minute in load_cumulative and next_minute in load_cumulative:
            load_delta = load_cumulative[minute] - load_cumulative[next_minute]
            load_deltas[minute] = max(0, load_delta)
        
        # Calculate car delta
        if minute in car_filled and next_minute in car_filled:
            car_delta = car_filled[minute] - car_filled[next_minute]
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
        if minute not in load_deltas:
            continue
            
        load_delta = load_deltas[minute]
        
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
        
        if debug and best_car_delta > 0:
            print(f"Minute {minute}: load={load_delta:.3f}, car={best_car_delta:.3f}, result={adjusted_delta:.3f}")
        
        total_energy += adjusted_delta
        result[minute] = total_energy
    
    return result


def simple_car_subtraction(load_cumulative, car_cumulative):
    """
    Simple car subtraction (current method) - direct subtraction of cumulative values.
    This is what Predbat currently does.
    """
    if not car_cumulative or not load_cumulative:
        return load_cumulative
    
    result = {}
    max_minute = max(load_cumulative.keys())
    total_energy = 0.0
    
    for minute in range(max_minute, -5, -5):
        next_minute = minute + 5
        
        if minute in load_cumulative and next_minute in load_cumulative:
            load_delta = load_cumulative[minute] - load_cumulative[next_minute]
        else:
            load_delta = 0.0
        
        if minute in car_cumulative and next_minute in car_cumulative:
            car_delta = car_cumulative[minute] - car_cumulative[next_minute]
        else:
            car_delta = 0.0
        
        adjusted_delta = max(0.0, load_delta - car_delta)
        total_energy += adjusted_delta
        result[minute] = total_energy
    
    return result


# Find the maximum minute to determine range
max_minute = max(load_minutes.keys()) if load_minutes else 0
if load_minutes_orig:
    max_minute = max(max_minute, max(load_minutes_orig.keys()))
if car_charging_energy:
    max_minute = max(max_minute, max(car_charging_energy.keys()))

# Function to convert cumulative to per-step energy
def cumulative_to_energy_per_step(cumulative_data, step=5):
    """Convert cumulative kWh dict to energy per step (going backwards in time)"""
    energy_per_step = {}
    
    if not cumulative_data:
        return energy_per_step
    
    # Data is in backwards format: minute 0 = now (high value), minute N = past (low value)
    # Delta = current - next (going backwards)
    for minute in sorted(cumulative_data.keys()):
        next_minute = minute + step
        if next_minute in cumulative_data:
            delta = cumulative_data[minute] - cumulative_data[next_minute]
            energy_per_step[minute] = max(0, delta)
    
    return energy_per_step

# Convert cumulative values to energy per 5-min step (for visualization only)
load_energy = cumulative_to_energy_per_step(load_minutes)
load_orig_energy = cumulative_to_energy_per_step(load_minutes_orig)
car_energy = cumulative_to_energy_per_step(car_charging_energy)

# Apply both subtraction methods if we have car charging data
# If load_minutes_orig is empty, reconstruct it by adding car charging back to load_minutes
if car_charging_energy and (not load_minutes_orig or len(load_minutes_orig) == 0):
    print("\n=== Reconstructing original load (load_minutes_orig is empty) ===")
    # Reconstruct original load by adding car charging back
    load_minutes_orig = {}
    max_minute = max(load_minutes.keys())
    
    # Work backwards from minute max_minute (past) to minute 0 (now)
    # Both are cumulative backwards: minute 0 = high value (now), minute N = low value (past)
    for minute in range(max_minute, -5, -5):
        next_minute = minute + 5
        
        # Get load delta for this period
        if minute in load_minutes and next_minute in load_minutes:
            load_delta = load_minutes[minute] - load_minutes[next_minute]
        else:
            load_delta = 0.0
        
        # Get car delta for this period
        if minute in car_charging_energy and next_minute in car_charging_energy:
            car_delta = car_charging_energy[minute] - car_charging_energy[next_minute]
        else:
            car_delta = 0.0
        
        # Original load = processed load + car charging
        orig_delta = load_delta + car_delta
        
        # Build cumulative going backwards
        if next_minute in load_minutes_orig:
            load_minutes_orig[minute] = load_minutes_orig[next_minute] + orig_delta
        else:
            load_minutes_orig[minute] = orig_delta
    
    print(f"Reconstructed {len(load_minutes_orig)} original load data points")
    
    # Recalculate energy per step for the reconstructed original
    load_orig_energy = cumulative_to_energy_per_step(load_minutes_orig)

if load_minutes_orig and car_charging_energy:
    print("\n=== Comparing Car Subtraction Methods ===")
    
    # Method 1: Simple subtraction (current Predbat method)
    print("Applying simple subtraction...")
    simple_result = simple_car_subtraction(load_minutes_orig, car_charging_energy)
    simple_energy = cumulative_to_energy_per_step(simple_result)
    
    # Method 2: Improved subtraction with interpolation and smoothing
    print("Applying improved subtraction...")
    improved_result = improved_car_subtraction(
        load_minutes_orig, 
        car_charging_energy,
        interpolate_gaps=True,
        max_gap_minutes=60,
        smoothing_window=5,
        debug=False
    )
    improved_energy = cumulative_to_energy_per_step(improved_result)
    
    # Calculate total energy for each method
    total_orig = sum(load_orig_energy.values()) if load_orig_energy else 0
    total_car = sum(car_energy.values()) if car_energy else 0
    total_simple = sum(simple_energy.values())
    total_improved = sum(improved_energy.values())
    total_current = sum(load_energy.values())
    
    print(f"\nEnergy totals:")
    print(f"  Original load (before car subtraction): {total_orig:.2f} kWh")
    print(f"  Car charging total:                     {total_car:.2f} kWh")
    print(f"  Simple subtraction result:              {total_simple:.2f} kWh")
    print(f"  Improved subtraction result:            {total_improved:.2f} kWh")
    print(f"  Current processed load (in data):       {total_current:.2f} kWh")
    print(f"\nCar energy recovered:")
    print(f"  Simple method:   {total_orig - total_simple:.2f} kWh ({100*(total_orig - total_simple)/total_car:.1f}% of car total)")
    print(f"  Improved method: {total_orig - total_improved:.2f} kWh ({100*(total_orig - total_improved)/total_car:.1f}% of car total)")
    print(f"  Current method:  {total_orig - total_current:.2f} kWh ({100*(total_orig - total_current)/total_car:.1f}% of car total)")
else:
    simple_result = None
    improved_result = None
    simple_energy = None
    improved_energy = None

# Create the plot with 3 panels if we have comparison data, otherwise 2 panels
if simple_result and improved_result:
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(16, 14))
else:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10))
    ax3 = None

# Plot 1: Cumulative values
minutes_sorted = sorted(load_minutes.keys())
hours = [-m / 60 for m in minutes_sorted]  # Negative for past

if load_minutes_orig:
    ax1.plot(hours, [load_minutes_orig[m] for m in minutes_sorted], 
             'b-', linewidth=1.5, label='Original Load (before car subtraction)', alpha=0.7)

ax1.plot(hours, [load_minutes[m] for m in minutes_sorted], 
         'g-', linewidth=1.5, label='Processed Load (after car subtraction)', alpha=0.9)

if car_charging_energy:
    car_minutes_sorted = sorted(car_charging_energy.keys())
    car_hours = [-m / 60 for m in car_minutes_sorted]
    ax1.plot(car_hours, [car_charging_energy[m] for m in car_minutes_sorted], 
             'r-', linewidth=1.0, label='Car Charging Energy', alpha=0.6)

ax1.axvline(x=0, color='black', linestyle='--', linewidth=2, label='Now', alpha=0.8)
ax1.set_xlabel('Hours from Now (negative = past)', fontsize=12)
ax1.set_ylabel('Cumulative Energy (kWh)', fontsize=12)
ax1.set_title('Cumulative Load Data (Backwards in Time)', fontsize=14, fontweight='bold')
ax1.legend(loc='upper left', fontsize=10)
ax1.grid(True, alpha=0.3)
ax1.set_xlim(-48, 0)  # Show only last 48 hours

# Plot 2: Energy per 5-minute step with method comparison
if load_orig_energy:
    load_orig_sorted = sorted(load_orig_energy.keys())
    load_orig_hours = [-m / 60 for m in load_orig_sorted]
    ax2.plot(load_orig_hours, [load_orig_energy[m] for m in load_orig_sorted], 
             'b-', linewidth=1.0, label='Original Load (before subtraction)', alpha=0.4)

if car_energy:
    car_sorted = sorted(car_energy.keys())
    car_hours = [-m / 60 for m in car_sorted]
    ax2.plot(car_hours, [car_energy[m] for m in car_sorted], 
             'r-', linewidth=1.0, label='Car Charging', alpha=0.5)

if load_energy:
    load_sorted = sorted(load_energy.keys())
    load_hours = [-m / 60 for m in load_sorted]
    ax2.plot(load_hours, [load_energy[m] for m in load_sorted], 
             'orange', linewidth=1.2, label='Current Predbat Result', alpha=0.7, linestyle='--')

if simple_energy:
    simple_sorted = sorted(simple_energy.keys())
    simple_hours = [-m / 60 for m in simple_sorted]
    ax2.plot(simple_hours, [simple_energy[m] for m in simple_sorted], 
             'purple', linewidth=1.2, label='Simple Subtraction', alpha=0.8, linestyle='-.')

if improved_energy and 0:
    improved_sorted = sorted(improved_energy.keys())
    improved_hours = [-m / 60 for m in improved_sorted]
    ax2.plot(improved_hours, [improved_energy[m] for m in improved_sorted], 
             'g-', linewidth=1.5, label='Improved Subtraction', alpha=0.9)

ax2.axvline(x=0, color='black', linestyle='--', linewidth=2, label='Now', alpha=0.8)
ax2.set_xlabel('Hours from Now (negative = past)', fontsize=12)
ax2.set_ylabel('Energy per 5-min Step (kWh)', fontsize=12)
ax2.set_title('Car Charging Subtraction Method Comparison', fontsize=14, fontweight='bold')
ax2.legend(loc='upper left', fontsize=10)
ax2.grid(True, alpha=0.3)
ax2.set_xlim(-48, 0)  # Show only last 48 hours

# Plot 3: Difference between methods (if comparison data exists)
if ax3 and simple_energy and improved_energy:
    # Calculate difference: improved - simple (positive means improved removes more)
    diff_minutes = sorted(set(simple_energy.keys()) & set(improved_energy.keys()))
    diff_hours = [-m / 60 for m in diff_minutes]
    diff_values = [improved_energy[m] - simple_energy[m] for m in diff_minutes]
    
    # Also show difference from current Predbat result if available
    if load_energy:
        current_diff_minutes = sorted(set(load_energy.keys()) & set(improved_energy.keys()))
        current_diff_hours = [-m / 60 for m in current_diff_minutes]
        current_diff_values = [improved_energy[m] - load_energy[m] for m in current_diff_minutes]
        ax3.plot(current_diff_hours, current_diff_values, 
                 'orange', linewidth=1.2, label='Improved - Current Predbat', alpha=0.7)
    
    ax3.plot(diff_hours, diff_values, 
             'purple', linewidth=1.5, label='Improved - Simple', alpha=0.9)
    ax3.axhline(y=0, color='gray', linestyle='-', linewidth=1, alpha=0.5)
    ax3.axvline(x=0, color='black', linestyle='--', linewidth=2, alpha=0.8)
    ax3.set_xlabel('Hours from Now (negative = past)', fontsize=12)
    ax3.set_ylabel('Energy Difference (kWh)', fontsize=12)
    ax3.set_title('Difference Between Methods (Positive = Improved removes more car energy)', fontsize=14, fontweight='bold')
    ax3.legend(loc='upper left', fontsize=10)
    ax3.grid(True, alpha=0.3)
    ax3.set_xlim(-48, 0)  # Show only last 48 hours
    
    # Add statistics box
    mean_diff = sum(diff_values) / len(diff_values) if diff_values else 0
    max_diff = max(diff_values) if diff_values else 0
    min_diff = min(diff_values) if diff_values else 0
    stats_text = f'Mean: {mean_diff:.4f} kWh\nMax: {max_diff:.4f} kWh\nMin: {min_diff:.4f} kWh'
    ax3.text(0.02, 0.98, stats_text, transform=ax3.transAxes, fontsize=10,
             verticalalignment='top', bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

# Add day markers (only for last 48 hours)
for day in range(-2, 1):  # -48h, -24h, 0h
    hour = day * 24
    if -48 <= hour <= 0:
        ax1.axvline(x=hour, color='gray', linestyle=':', linewidth=0.5, alpha=0.5)
        ax2.axvline(x=hour, color='gray', linestyle=':', linewidth=0.5, alpha=0.5)
        if ax3:
            ax3.axvline(x=hour, color='gray', linestyle=':', linewidth=0.5, alpha=0.5)

plt.tight_layout()
plt.savefig('load_data_plot.png', dpi=150, bbox_inches='tight')
print("\nChart saved to load_data_plot.png")
print(f"\nData summary:")
print(f"  Load minutes: {len(load_minutes)} points")
print(f"  Load minutes original: {len(load_minutes_orig)} points")
print(f"  Car charging: {len(car_charging_energy)} points")
print(f"  Time range: {max_minute / 60:.1f} hours ({max_minute / (60 * 24):.1f} days)")

