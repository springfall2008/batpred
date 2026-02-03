# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test rate_store persistent rate tracking with finalization
# -----------------------------------------------------------------------------

import os
import json
import shutil
from datetime import datetime, timedelta
from rate_store import RateStore
from persistent_store import PersistentStore


def run_rate_store_tests():
    """
    Run comprehensive tests for rate persistence and finalization
    """
    print("=" * 80)
    print("Testing Rate Store Persistence and Finalization")
    print("=" * 80)
    
    # Create test directory
    test_dir = "test_rate_store_temp"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir)
    
    try:
        # Test 1: Basic persistence
        print("\n*** Test 1: Basic rate persistence ***")
        test_basic_persistence(test_dir)
        
        # Test 2: Finalization
        print("\n*** Test 2: Rate finalization ***")
        test_finalization(test_dir)
        
        # Test 3: Rehydration after restart
        print("\n*** Test 3: Rehydration after restart ***")
        test_rehydration(test_dir)
        
        # Test 4: Override priority
        print("\n*** Test 4: Override priority (manual > automatic > initial) ***")
        test_override_priority(test_dir)
        
        # Test 5: Finalized rates resist new API data
        print("\n*** Test 5: Finalized rates resist fresh API data ***")
        test_finalized_resistance(test_dir)
        
        # Test 6: Automatic override removal detection
        print("\n*** Test 6: Automatic override removal detection ***")
        test_override_removal(test_dir)
        
        # Test 7: Cleanup old files
        print("\n*** Test 7: Cleanup old files ***")
        test_cleanup(test_dir)
        
        # Test 8: Slot interval mismatch handling
        print("\n*** Test 8: Slot interval mismatch handling ***")
        test_interval_mismatch(test_dir)
        
        print("\n" + "=" * 80)
        print("All rate store tests PASSED")
        print("=" * 80)
        return True
        
    except AssertionError as e:
        print(f"\n*** TEST FAILED: {e} ***")
        return False
    finally:
        # Cleanup
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)


def test_basic_persistence(test_dir):
    """Test basic write and read of rates"""
    
    # Create mock base object
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720  # 12:00
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default
    
    base = MockBase()
    store = RateStore(base, save_dir=test_dir)
    
    # Write some base rates
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Write rates for 00:00, 00:30, 01:00
    store.write_base_rate(today, 0, 10.0, 5.0)    # import=10p, export=5p
    store.write_base_rate(today, 30, 12.0, 6.0)   # import=12p, export=6p
    store.write_base_rate(today, 60, 15.0, 7.0)   # import=15p, export=7p
    
    # Read back
    rate_import_0 = store.get_rate(today, 0, is_import=True)
    rate_export_0 = store.get_rate(today, 0, is_import=False)
    rate_import_30 = store.get_rate(today, 30, is_import=True)
    rate_export_60 = store.get_rate(today, 60, is_import=False)
    
    assert rate_import_0 == 10.0, f"Expected 10.0, got {rate_import_0}"
    assert rate_export_0 == 5.0, f"Expected 5.0, got {rate_export_0}"
    assert rate_import_30 == 12.0, f"Expected 12.0, got {rate_import_30}"
    assert rate_export_60 == 7.0, f"Expected 7.0, got {rate_export_60}"
    
    # Check file was created
    date_str = today.strftime("%Y_%m_%d")
    filepath = os.path.join(test_dir, f"rates_{date_str}.json")
    assert os.path.exists(filepath), "Rate file not created"
    
    print("  ✓ Basic persistence working")


def test_finalization(test_dir):
    """Test that slots get finalized 5 minutes after start"""
    
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 725  # 12:05 - 5 minutes into 12:00 slot
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return default
    
    base = MockBase()
    store = RateStore(base, save_dir=test_dir)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Write rates for multiple slots
    store.write_base_rate(today, 0, 10.0, 5.0)      # 00:00 slot (should finalize)
    store.write_base_rate(today, 30, 12.0, 6.0)     # 00:30 slot (should finalize)
    store.write_base_rate(today, 690, 15.0, 7.0)    # 11:30 slot (should finalize)
    store.write_base_rate(today, 720, 20.0, 10.0)   # 12:00 slot (should finalize at 12:05)
    store.write_base_rate(today, 750, 25.0, 12.0)   # 12:30 slot (should NOT finalize yet)
    
    # Finalize slots at current time (12:05)
    finalized_count = store.finalize_slots(today, base.minutes_now)
    
    # Should finalize 00:00, 00:30, 11:30, 12:00 (4 slots)
    assert finalized_count == 4, f"Expected 4 slots finalized, got {finalized_count}"
    
    # Check finalization status in the file
    date_str = today.strftime("%Y_%m_%d")
    filepath = os.path.join(test_dir, f"rates_{date_str}.json")
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    assert data['rates_import']['00:00']['finalized'] == True, "00:00 should be finalized"
    assert data['rates_import']['12:00']['finalized'] == True, "12:00 should be finalized"
    assert data['rates_import']['12:30']['finalized'] == False, "12:30 should NOT be finalized"
    
    print("  ✓ Finalization working correctly")


def test_rehydration(test_dir):
    """Test that rates survive restart and are reloaded"""
    
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return 7 if key == "rate_retention_days" else default
    
    # First instance - write data
    base1 = MockBase()
    store1 = RateStore(base1, save_dir=test_dir)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Write and finalize some rates
    store1.write_base_rate(today, 0, 10.0, 5.0)
    store1.write_base_rate(today, 30, 12.0, 6.0)
    store1.update_auto_override(today, 60, 8.0, None, "IOG")  # Cheap IOG slot
    store1.update_manual_override(today, 90, 20.0, None)      # Manual override
    store1.finalize_slots(today, 100)  # Finalize all past slots
    
    # Verify initial state
    assert store1.get_rate(today, 0, is_import=True) == 10.0
    assert store1.get_rate(today, 30, is_import=True) == 12.0
    assert store1.get_rate(today, 60, is_import=True) == 8.0   # IOG override
    assert store1.get_rate(today, 90, is_import=True) == 20.0  # Manual override
    
    # Simulate restart - create new instance
    base2 = MockBase()
    store2 = RateStore(base2, save_dir=test_dir)
    
    # Manually load the data (simulating what constructor does)
    store2.load_rates(today)
    
    # Verify data was restored
    assert store2.get_rate(today, 0, is_import=True) == 10.0, "Initial rate not restored"
    assert store2.get_rate(today, 30, is_import=True) == 12.0, "Initial rate not restored"
    assert store2.get_rate(today, 60, is_import=True) == 8.0, "IOG override not restored"
    assert store2.get_rate(today, 90, is_import=True) == 20.0, "Manual override not restored"
    
    print("  ✓ Rehydration after restart working")


def test_override_priority(test_dir):
    """Test that manual > automatic > initial priority is respected"""
    
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return default
    
    base = MockBase()
    store = RateStore(base, save_dir=test_dir)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Test slot at 00:00
    # Start with initial rate
    store.write_base_rate(today, 0, 10.0, 5.0)
    assert store.get_rate(today, 0, is_import=True) == 10.0, "Initial rate should be 10.0"
    
    # Apply automatic override (IOG)
    store.update_auto_override(today, 0, 7.5, None, "IOG")
    assert store.get_rate(today, 0, is_import=True) == 7.5, "Automatic override should be 7.5"
    
    # Apply manual override (should take priority)
    store.update_manual_override(today, 0, 15.0, None)
    assert store.get_rate(today, 0, is_import=True) == 15.0, "Manual override should be 15.0"
    
    # Clear manual override
    store.update_manual_override(today, 0, None, None)
    assert store.get_rate(today, 0, is_import=True) == 7.5, "Should fall back to automatic override (7.5)"
    
    # Clear automatic override
    store.update_auto_override(today, 0, None, None, "IOG")
    assert store.get_rate(today, 0, is_import=True) == 10.0, "Should fall back to initial rate (10.0)"
    
    print("  ✓ Override priority working correctly")


def test_finalized_resistance(test_dir):
    """Test that finalized rates cannot be changed by new overrides"""
    
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return default
    
    base = MockBase()
    store = RateStore(base, save_dir=test_dir)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Write initial rate and automatic override for 00:00 slot
    store.write_base_rate(today, 0, 10.0, 5.0)
    store.update_auto_override(today, 0, 7.5, None, "IOG")
    assert store.get_rate(today, 0, is_import=True) == 7.5, "Should have IOG rate initially"
    
    # Finalize the slot
    store.finalize_slots(today, 10)  # 10 minutes past midnight, so 00:00 is finalized
    
    # Try to apply new override (should be ignored for finalized slot)
    store.update_auto_override(today, 0, 20.0, None, "IOG")
    assert store.get_rate(today, 0, is_import=True) == 7.5, "Finalized rate should not change from IOG override"
    
    # Try manual override (should also be ignored)
    store.update_manual_override(today, 0, 25.0, None)
    assert store.get_rate(today, 0, is_import=True) == 7.5, "Finalized rate should not change from manual override"
    
    # Non-finalized slot should still accept changes
    store.write_base_rate(today, 720, 12.0, 6.0)  # 12:00 slot (not finalized)
    store.update_auto_override(today, 720, 9.0, None, "IOG")
    assert store.get_rate(today, 720, is_import=True) == 9.0, "Non-finalized slot should accept override"
    
    print("  ✓ Finalized rates resist changes correctly")


def test_override_removal(test_dir):
    """Test detection of automatic override removal (e.g., IOG slot removed)"""
    
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return default
    
    base = MockBase()
    store = RateStore(base, save_dir=test_dir)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Set up a future slot with IOG override
    store.write_base_rate(today, 1440, 15.0, 7.0)  # Tomorrow 00:00
    store.update_auto_override(today, 1440, 7.5, None, "IOG")
    assert store.get_rate(today, 1440, is_import=True) == 7.5, "Should have IOG override"
    
    # Remove the IOG override (simulating Octopus removing the slot)
    store.update_auto_override(today, 1440, None, None, "IOG")
    assert store.get_rate(today, 1440, is_import=True) == 15.0, "Should fall back to base rate"
    
    # Verify in file
    date_str = today.strftime("%Y_%m_%d")
    filepath = os.path.join(test_dir, f"rates_{date_str}.json")
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    assert data['rates_import']['24:00']['automatic'] is None, "Automatic override should be cleared"
    
    print("  ✓ Override removal detection working")


def test_cleanup(test_dir):
    """Test cleanup of old rate files"""
    
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return default
    
    base = MockBase()
    store = RateStore(base, save_dir=test_dir)
    
    # Create old rate files
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    for days_ago in range(10):
        old_date = today - timedelta(days=days_ago)
        store.write_base_rate(old_date, 0, 10.0, 5.0)
    
    # Should have 10 files
    files = [f for f in os.listdir(test_dir) if f.startswith("rates_") and f.endswith(".json")]
    assert len(files) == 10, f"Expected 10 files, got {len(files)}"
    
    # Cleanup with 7 day retention
    removed = store.cleanup_old_files(retention_days=7)
    
    # Should remove 3 files (8, 9, 10 days old)
    assert removed == 3, f"Expected 3 files removed, got {removed}"
    
    # Should have 7 files remaining
    files = [f for f in os.listdir(test_dir) if f.startswith("rates_") and f.endswith(".json")]
    assert len(files) == 7, f"Expected 7 files remaining, got {len(files)}"
    
    print("  ✓ Cleanup working correctly")


def test_interval_mismatch(test_dir):
    """Test handling of plan_interval_minutes mismatch"""
    
    class MockBase:
        def __init__(self, interval=30):
            self.plan_interval_minutes = interval
            self.minutes_now = 720
            
        def log(self, msg):
            print(f"  {msg}")
            
        def get_arg(self, key, default):
            return default
    
    # Create file with 30-minute intervals
    base1 = MockBase(interval=30)
    store1 = RateStore(base1, save_dir=test_dir)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    store1.write_base_rate(today, 0, 10.0, 5.0)
    store1.write_base_rate(today, 30, 12.0, 6.0)
    
    # Try to load with different interval (should backup and restart)
    base2 = MockBase(interval=60)
    store2 = RateStore(base2, save_dir=test_dir)
    
    # Should have created backup
    date_str = today.strftime("%Y_%m_%d")
    backup_path = os.path.join(test_dir, f"rates_{date_str}.json.bak")
    assert os.path.exists(backup_path), "Backup file should be created on interval mismatch"
    
    # Load data - should have new empty structure with 60-minute interval
    data = store2.load_rates(today)
    assert data['plan_interval_minutes'] == 60, "Should have new interval"
    
    print("  ✓ Interval mismatch handled correctly")


if __name__ == "__main__":
    success = run_rate_store_tests()
    exit(0 if success else 1)
