#!/usr/bin/env python3
"""
Simple test to process load_power_data.yaml through fill_load_from_power
"""

import sys
import os
import yaml
import matplotlib.pyplot as plt

# Add the apps/predbat directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "apps", "predbat"))

from fetch import Fetch


class TestFetch(Fetch):
    """Test class that inherits from Fetch to access its methods"""

    def __init__(self):
        # Initialize minimal required attributes
        self.log_messages = []
        self.forecast_minutes = 24 * 60  # 24 hours
        self.plan_interval_minutes = 30

    def log(self, message):
        """Capture log messages"""
        self.log_messages.append(message)
        print(message)

    def get_arg(self, name, default=None):
        """Mock get_arg to return default values"""
        return default


def main():
    """Load data from YAML, process it, and save results"""

    # Load input data
    print("Loading load_power_data.yaml...")
    try:
        with open("load_power_data.yaml", "r") as file:
            data = yaml.safe_load(file)
    except FileNotFoundError:
        print("Error: load_power_data.yaml not found in current directory")
        return 1

    load_minutes = data.get("load_minutes", {})
    load_power_data = data.get("load_power_data", {})

    print(f"Loaded {len(load_minutes)} load data points")
    print(f"Loaded {len(load_power_data)} power data points")

    # Create test fetch instance
    fetch = TestFetch()

    # Process the data
    print("\nProcessing data through fill_load_from_power...")
    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # Save results
    output_file = "load_power_data_processed.yaml"
    print(f"\nSaving results to {output_file}...")
    with open(output_file, "w") as file:
        yaml.dump({"original_load_minutes": load_minutes, "load_power_data": load_power_data, "processed_load_minutes": result}, file, default_flow_style=False)

    print(f"\n✓ Successfully processed data")
    print(f"✓ Results saved to {output_file}")

    # Show summary
    if load_minutes and result:
        print("\nSummary:")
        print(f"  Original first value (minute 0): {load_minutes.get(0, 'N/A')}")
        print(f"  Processed first value (minute 0): {result.get(0, 'N/A')}")
        if len(load_minutes) > 30:
            print(f"  Original value at minute 30: {load_minutes.get(30, 'N/A')}")
            print(f"  Processed value at minute 30: {result.get(30, 'N/A')}")

    # Create visualization
    print("\nCreating visualization...")
    create_chart(load_minutes, load_power_data, result)

    return 0


def create_chart(original_load, power_data, processed_load):
    """Create a chart showing before/after load curves and power data"""

    # Get the data range to plot (first 1440*2 minutes = 2 days)
    max_minutes = min(1440 * 2, max(max(original_load.keys()) if original_load else 0, max(processed_load.keys()) if processed_load else 0))

    # Prepare data for plotting
    minutes = list(range(0, max_minutes + 1))
    original_values = [original_load.get(m, 0) for m in minutes]
    processed_values = [processed_load.get(m, 0) for m in minutes]
    power_values = [power_data.get(m, 0) / 1000.0 for m in minutes]  # Convert W to kW

    # Calculate power from processed load (5-minute differences)
    # Since data goes backwards, load[m] - load[m+5] gives energy consumed
    # Multiply by 12 to convert kWh over 5 min to kW (60 min/hr ÷ 5 min = 12)
    original_load_as_power_values = []
    processed_power_values = []
    for m in minutes:
        if m + 5 <= max_minutes:
            energy_5min = processed_load.get(m, 0) - processed_load.get(m + 5, 0)
            power_kw = energy_5min * 12.0  # Convert to kW
            processed_power_values.append(power_kw)

            energy_5min_orig = original_load.get(m, 0) - original_load.get(m + 5, 0)
            power_kw_orig = energy_5min_orig * 12.0  # Convert to
            original_load_as_power_values.append(power_kw_orig)
        else:
            processed_power_values.append(0)
            original_load_as_power_values.append(0)

    # Create figure with two subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))

    # Top subplot: Load curves (cumulative kWh)
    ax1.plot(minutes, original_values, "b-", linewidth=1, label="Original Load (gap-filled)", alpha=0.7)
    ax1.plot(minutes, processed_values, "r-", linewidth=1, label="Processed Load (power-smoothed)", alpha=0.8)
    ax1.set_xlabel("Minutes (0 = now, going backwards in time)", fontsize=10)
    ax1.set_ylabel("Cumulative Load (kWh)", fontsize=10)
    ax1.set_title("Load Data: Before vs After Power Integration", fontsize=12, fontweight="bold")
    ax1.legend(loc="upper right")
    ax1.grid(True, alpha=0.3)
    ax1.invert_xaxis()  # Invert x-axis since minute 0 is now

    # Bottom subplot: Power data comparison
    ax2.plot(minutes, power_values, "g-", linewidth=0.5, label="Original Power Data (kW)", alpha=0.6)
    ax2.plot(minutes, original_load_as_power_values, "b-", linewidth=1, label="Power from Original Load (kW)", alpha=0.7)
    ax2.plot(minutes, processed_power_values, "r-", linewidth=1, label="Power from Processed Load (kW)", alpha=0.8)
    ax2.set_xlabel("Minutes (0 = now, going backwards in time)", fontsize=10)
    ax2.set_ylabel("Power (kW)", fontsize=10)
    ax2.set_title("Power Comparison: Original vs Derived from Smoothed Load", fontsize=12, fontweight="bold")
    ax2.legend(loc="upper right")
    ax2.grid(True, alpha=0.3)
    ax2.invert_xaxis()  # Invert x-axis since minute 0 is now

    plt.tight_layout()

    # Save the chart
    chart_file = "load_power_chart.png"
    plt.savefig(chart_file, dpi=150, bbox_inches="tight")
    print(f"✓ Chart saved to {chart_file}")

    # Also show the chart
    plt.show()
    print("✓ Chart displayed")


if __name__ == "__main__":
    sys.exit(main())
