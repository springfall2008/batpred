#!/usr/bin/env python3
"""
Standalone test for previous_days_modal_filter function using real Fetch class
"""
import sys
import os
import yaml

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'apps', 'predbat'))

from fetch import Fetch
from const import PREDICT_STEP


class TestFetch(Fetch):
    """Minimal test subclass of Fetch with required dependencies"""
    def __init__(self):
        # Required attributes for previous_days_modal_filter
        self.days_previous = [5, 6]
        self.days_previous_weight = [1, 1]
        self.load_minutes_age = 1  # Only 1 day of data
        self.load_filter_modal = False  # Disable modal filter to see gap detection
        self.car_charging_hold = False
        self.car_charging_energy = None
        self.iboost_energy_subtract = False
        self.iboost_energy_today = None
        self.car_charging_threshold = 7.4
        self.car_charging_rate = [7.4]
        self.base_load = 0.0
        self.plan_interval_minutes = 30
        
    def log(self, msg):
        print(f"LOG: {msg}")
        
    def get_arg(self, key, default=None):
        if key == "load_filter_threshold":
            return self.plan_interval_minutes
        return default


def main():
    """Main test function"""
    print("=" * 80)
    print("Testing previous_days_modal_filter function (using real Fetch class)")
    print("=" * 80)
    
    # Load the test data
    yaml_file = "load_previous_days.yaml"
    print(f"\nLoading data from {yaml_file}...")
    
    with open(yaml_file, 'r') as f:
        data = yaml.safe_load(f)
    
    print(f"Loaded {len(data)} data points")
    
    # Show sample data for each day
    print("\nData samples:")
    for day in [5, 6]:
        full_days = 24 * 60 * (day - 1)
        minute_check = full_days
        print(f"  Day {day} (minute {minute_check}): {data.get(minute_check, 'N/A')}")
        print(f"  Day {day} (minute {minute_check + 60}): {data.get(minute_check + 60, 'N/A')}")
        
        # Check if day is all zeros by sampling every 100 minutes
        sample_count = 0
        zero_count = 0
        for m in range(0, 24 * 60, 100):
            sample_minute = full_days + m
            value = data.get(sample_minute, 0)
            sample_count += 1
            if value == 0:
                zero_count += 1
        print(f"  Day {day} zero sample ratio: {zero_count}/{sample_count}")
        
    # Show what minute ranges will be checked with load_minutes_age = 1
    print("\nWith load_minutes_age = 1, use_days calculation:")
    for day in [5, 6]:
        use_days = max(min(day, 1), 1)  # This is the formula from the code
        full_days = 24 * 60 * (use_days - 1)
        print(f"  Day {day}: use_days={use_days}, full_days={full_days}")
        print(f"    Will check data from minute {full_days} to {full_days + 24*60}")
        print(f"    Sample values: {data.get(full_days, 'N/A')}, {data.get(full_days + 100, 'N/A')}, {data.get(full_days + 200, 'N/A')}")
    
    # Create test Fetch instance
    print("\nCreating TestFetch instance...")
    fetch = TestFetch()
    print(f"  days_previous: {fetch.days_previous}")
    print(f"  load_minutes_age: {fetch.load_minutes_age}")
    print(f"  load_filter_modal: {fetch.load_filter_modal}")
    
    # Run the REAL previous_days_modal_filter function from Fetch class
    print("\n" + "=" * 80)
    print("Running previous_days_modal_filter from Fetch class...")
    print("=" * 80)
    fetch.previous_days_modal_filter(data)
    
    print("\n" + "=" * 80)
    print("Results:")
    print("=" * 80)
    print(f"  days_previous after filter: {fetch.days_previous}")
    print(f"  days_previous_weight after filter: {fetch.days_previous_weight}")
    
    # Check what happened to day 6
    if 6 in fetch.days_previous:
        print("\n  ✗ ISSUE: Day 6 was NOT removed (should have been removed as all zeros)")
    else:
        print("\n  ✓ OK: Day 6 was removed as expected")
        

if __name__ == "__main__":
    main()
