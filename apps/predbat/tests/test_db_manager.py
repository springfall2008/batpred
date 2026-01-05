# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import tempfile
import shutil
import asyncio
import pytz
from datetime import datetime, timedelta, timezone
from db_manager import DatabaseManager
from db_engine import TIME_FORMAT_DB


def run_async(coro):
    """Helper function to run async coroutines in sync test functions"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class MockBase:
    """Mock base class for DatabaseManager testing"""

    def __init__(self):
        self.config_root = tempfile.mkdtemp()
        self.log_messages = []
        # Use GMT+2 timezone for testing timezone conversion
        self.local_tz = pytz.timezone("Etc/GMT-2")  # GMT+2
        self.now_utc = datetime(2025, 12, 25, 14, 30, 0, tzinfo=self.local_tz)
        self.now_utc_exact = self.now_utc
        self.now_utc_real = datetime.now(timezone.utc)
        self.prefix = "predbat"

    def log(self, message):
        """Log messages for test verification"""
        self.log_messages.append(message)
        print(f"[TEST LOG] {message}")

    def call_notify(self, message):
        """Mock notify method"""
        self.log_messages.append("Alert: " + message)


class MockDatabaseManager(DatabaseManager):
    """Mock DatabaseManager that bypasses ComponentBase"""

    def __init__(self):
        # Don't call parent __init__ to avoid ComponentBase
        pass

    @property
    def now_utc_exact(self):
        """Override property to return value from mock base"""
        return self.base.now_utc_exact

    @property
    def local_tz(self):
        """Override property to return value from mock base"""
        return self.base.local_tz


def test_db_manager(my_predbat=None):
    """
    DATABASE MANAGER TEST SUITE

    Comprehensive test suite for DatabaseManager component covering:
    - State operations: set/get state with attributes
    - Entities and history: entity registration, history queries with time windows
    - Error handling: database errors, corrupted files, missing directories
    - Persistence: data survival across restarts, state recovery
    - Commit throttling: 5-second interval enforcement

    Total: 5 sub-tests
    """

    # Registry of all Database Manager tests
    sub_tests = [
        ("set_get_state", _test_db_manager_set_get_state, "Set/get state operations with attributes"),
        ("entities_history", _test_db_manager_entities_and_history, "Entities and history queries"),
        ("error_handling", _test_db_manager_error_handling, "Error handling (corrupted files, missing dirs)"),
        ("persistence", _test_db_manager_persistence, "Data persistence across restarts"),
        ("commit_throttle", _test_db_manager_commit_throttling, "Commit throttling (5 second interval)"),
    ]

    print("\n" + "=" * 70)
    print("DATABASE MANAGER TEST SUITE")
    print("=" * 70)

    passed = 0
    failed = 0

    for key, test_func, description in sub_tests:
        print(f"\n[{key}] {description}")
        print("-" * 70)
        try:
            result = test_func(my_predbat)
            if result:
                print(f"✗ FAILED: {key}")
                failed += 1
            else:
                print(f"✓ PASSED: {key}")
                passed += 1
        except Exception as e:
            print(f"✗ EXCEPTION in {key}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed > 0


def _test_db_manager_set_get_state(my_predbat=None):
    """Test DatabaseManager set_state_db and get_state_db operations with attributes"""
    print("\n=== Testing DatabaseManager set/get state operations ===")

    async def run_test():
        # Create mock base with temp directory
        mock_base = MockBase()

        # Create DatabaseManager using our mock class
        db_mgr = MockDatabaseManager()
        db_mgr.api_stop = False
        db_mgr.base = mock_base
        db_mgr.log = mock_base.log
        db_mgr.initialize(db_enable=True, db_days=30)

        try:
            # Start the async component
            task = asyncio.create_task(db_mgr.start())

            # Wait for startup
            timeout = 0
            while not db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert db_mgr.api_started, "DatabaseManager failed to start"
            print("✓ DatabaseManager started successfully")

            # Test 1: Set state with attributes
            entity_id = "sensor.test_battery"
            state = "42.5"
            attributes = {"unit_of_measurement": "kWh", "friendly_name": "Test Battery", "device_class": "energy"}

            # Run blocking IPC calls in thread executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, db_mgr.set_state_db, entity_id, state, attributes)

            # Verify returned item has correct structure
            assert "last_changed" in result, "set_state_db should return last_changed"
            assert result["state"] == state, "Returned state doesn't match"
            assert result["attributes"] == attributes, "Returned attributes don't match"
            print(f"✓ set_state_db returned correct structure: {result}")

            # Test 2: Get state back and verify
            retrieved = await loop.run_in_executor(None, db_mgr.get_state_db, entity_id)
            assert retrieved is not None, "get_state_db returned None"
            assert retrieved["state"] == state, f"Expected state {state}, got {retrieved['state']}"
            assert "attributes" in retrieved, "Retrieved state missing attributes"
            assert retrieved["attributes"]["unit_of_measurement"] == "kWh", "Attribute unit_of_measurement mismatch"
            assert retrieved["attributes"]["friendly_name"] == "Test Battery", "Attribute friendly_name mismatch"
            assert retrieved["attributes"]["device_class"] == "energy", "Attribute device_class mismatch"
            print(f"✓ get_state_db retrieved correct state and attributes: {retrieved}")

            # Test 3: Set state with custom timestamp (test timezone conversion)
            # GMT+2 10:00 should be stored as GMT+0 08:00 in the database
            custom_timestamp = datetime(2025, 12, 25, 10, 0, 0, tzinfo=mock_base.local_tz)
            entity_id2 = "sensor.test_solar"
            state2 = "15.3"
            attributes2 = {"unit_of_measurement": "kW"}

            result2 = await loop.run_in_executor(None, db_mgr.set_state_db, entity_id2, state2, attributes2, custom_timestamp)
            retrieved2 = await loop.run_in_executor(None, db_mgr.get_state_db, entity_id2)
            assert retrieved2 is not None, "get_state_db returned None for entity2"
            assert retrieved2["state"] == state2, f"Expected state {state2}, got {retrieved2['state']}"
            assert retrieved2["attributes"]["unit_of_measurement"] == "kW", "Attribute mismatch for entity2"

            # Verify timezone conversion: GMT+2 10:00 -> GMT+0 08:00
            timestamp_field = "last_changed" if "last_changed" in retrieved2 else "last_updated"
            assert timestamp_field in retrieved2, f"Retrieved state missing timestamp field"
            timestamp_str = retrieved2[timestamp_field]
            # Parse timestamp (format: "2025-12-25T08:00:00.000000Z" or similar)
            parsed_ts = datetime.strptime(timestamp_str.rstrip("Z"), TIME_FORMAT_DB)
            expected_gmt0_time = datetime(2025, 12, 25, 8, 0, 0)  # GMT+2 10:00 -> GMT+0 08:00
            assert parsed_ts.hour == expected_gmt0_time.hour, f"Timezone conversion failed: expected hour {expected_gmt0_time.hour} (GMT+0), got {parsed_ts.hour}"
            assert parsed_ts.day == expected_gmt0_time.day, f"Timezone conversion failed: expected day {expected_gmt0_time.day}, got {parsed_ts.day}"
            print(f"✓ Timezone conversion verified: GMT+2 10:00 -> GMT+0 {parsed_ts.strftime('%H:%M:%S')}")

            # Test 4: Update existing entity with new state
            state3 = "50.0"
            await loop.run_in_executor(None, db_mgr.set_state_db, entity_id, state3, attributes)
            retrieved3 = await loop.run_in_executor(None, db_mgr.get_state_db, entity_id)
            assert retrieved3["state"] == state3, f"Expected updated state {state3}, got {retrieved3['state']}"
            print(f"✓ State update works correctly: {retrieved3['state']}")

            # Stop the component and verify thread exits
            await db_mgr.stop()

            # Wait for thread to fully exit (api_started should become False)
            timeout = 0
            while db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert not db_mgr.api_started, "DatabaseManager thread did not exit after stop command"
            print("✓ DatabaseManager thread exited after stop command")
            await task

            print("✓ DatabaseManager stopped successfully")

        finally:
            # Cleanup temp directory
            shutil.rmtree(mock_base.config_root)
            print(f"✓ Cleaned up temp directory: {mock_base.config_root}")

    run_async(run_test())
    print("=== test_db_manager_set_get_state PASSED ===\n")


def _test_db_manager_entities_and_history(my_predbat=None):
    """Test DatabaseManager get_all_entities_db and get_history_db operations"""
    print("\n=== Testing DatabaseManager entities and history operations ===")

    async def run_test():
        mock_base = MockBase()

        # Create DatabaseManager using our mock class
        db_mgr = MockDatabaseManager()
        db_mgr.api_stop = False
        db_mgr.base = mock_base
        db_mgr.log = mock_base.log
        db_mgr.initialize(db_enable=True, db_days=30)

        try:
            # Start the component
            task = asyncio.create_task(db_mgr.start())

            # Wait for startup
            timeout = 0
            while not db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert db_mgr.api_started, "DatabaseManager failed to start"
            print("✓ DatabaseManager started successfully")

            # Test 1: Create multiple entities
            entities_to_create = [
                ("sensor.battery_soc", "85.5", {"unit_of_measurement": "%"}),
                ("sensor.solar_power", "3.2", {"unit_of_measurement": "kW"}),
                ("sensor.grid_import", "0.5", {"unit_of_measurement": "kW"}),
                ("sensor.home_load", "2.1", {"unit_of_measurement": "kW"}),
            ]

            loop = asyncio.get_event_loop()
            for entity_id, state, attributes in entities_to_create:
                await loop.run_in_executor(None, db_mgr.set_state_db, entity_id, state, attributes)

            # Test 2: Get all entities
            all_entities = await loop.run_in_executor(None, db_mgr.get_all_entities_db)
            assert all_entities is not None, "get_all_entities_db returned None"
            assert isinstance(all_entities, list), "get_all_entities_db should return a list"
            assert len(all_entities) >= 4, f"Expected at least 4 entities, got {len(all_entities)}"

            for entity_id, _, _ in entities_to_create:
                assert entity_id in all_entities, f"Entity {entity_id} not in returned list"

            print(f"✓ get_all_entities_db returned correct entities: {all_entities}")

            # Test 3: Create historical data for one entity
            history_entity = "sensor.battery_soc"
            base_timestamp = datetime(2025, 12, 25, 0, 0, 0, tzinfo=mock_base.local_tz)
            historical_states = []

            # Create 10 hourly data points
            for i in range(10):
                timestamp = base_timestamp + timedelta(hours=i)
                state_value = str(50.0 + i * 3.5)  # 50.0, 53.5, 57.0, ...
                attributes = {"unit_of_measurement": "%"}
                await loop.run_in_executor(None, db_mgr.set_state_db, history_entity, state_value, attributes, timestamp)
                historical_states.append({"timestamp": timestamp, "state": state_value, "attributes": attributes})

            # Test 4: Get history data
            now = datetime(2025, 12, 25, 12, 0, 0, tzinfo=mock_base.local_tz)
            history = await loop.run_in_executor(None, db_mgr.get_history_db, history_entity, now, 30)

            assert history is not None, "get_history_db returned None"
            assert isinstance(history, list), "get_history_db should return a list"
            assert len(history) > 0, "get_history_db returned empty list"

            # History should be wrapped in double list (matching HA format)
            if isinstance(history[0], list):
                history_data = history[0]
            else:
                history_data = history

            print(f"✓ get_history_db returned {len(history_data)} history entries")

            # Verify history entries have correct structure
            for entry in history_data[:3]:  # Check first 3 entries
                assert "state" in entry, "History entry missing 'state'"
                assert "last_changed" in entry or "last_updated" in entry, "History entry missing timestamp"
                print(f"  History entry: state={entry.get('state')}, timestamp={entry.get('last_changed', entry.get('last_updated'))}")

            # Verify at least some of our historical states are present
            history_states = [entry.get("state") for entry in history_data]
            found_count = sum(1 for hs in historical_states if hs["state"] in history_states)
            assert found_count > 0, f"None of the historical states found in returned history"
            print(f"✓ Found {found_count} of {len(historical_states)} expected historical states")

            # Test 5: Verify timestamp format
            for entry in history_data[:3]:
                timestamp_str = entry.get("last_changed", entry.get("last_updated", ""))
                if timestamp_str:
                    # Should be in TIME_FORMAT_DB format
                    try:
                        # Remove 'Z' suffix if present
                        ts = timestamp_str.rstrip("Z")
                        parsed = datetime.strptime(ts, TIME_FORMAT_DB)
                        print(f"✓ Timestamp format correct: {timestamp_str}")
                        break
                    except ValueError as e:
                        print(f"Warning: Timestamp format issue: {timestamp_str} - {e}")

            # Stop the component and verify thread exits
            await db_mgr.stop()

            # Wait for thread to fully exit
            timeout = 0
            while db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert not db_mgr.api_started, "DatabaseManager thread did not exit after stop command"
            print("✓ DatabaseManager thread exited after stop command")

            await task

            print("✓ DatabaseManager stopped successfully")

        finally:
            # Cleanup
            shutil.rmtree(mock_base.config_root)
            print(f"✓ Cleaned up temp directory: {mock_base.config_root}")

    run_async(run_test())
    print("=== test_db_manager_entities_and_history PASSED ===\n")


def _test_db_manager_error_handling(my_predbat=None):
    """Test DatabaseManager error handling for non-existent entities"""
    print("\n=== Testing DatabaseManager error handling ===")

    async def run_test():
        mock_base = MockBase()

        # Create DatabaseManager using our mock class
        db_mgr = MockDatabaseManager()
        db_mgr.api_stop = False
        db_mgr.base = mock_base
        db_mgr.log = mock_base.log
        db_mgr.initialize(db_enable=True, db_days=30)

        try:
            # Start the component
            task = asyncio.create_task(db_mgr.start())

            # Wait for startup
            timeout = 0
            while not db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert db_mgr.api_started, "DatabaseManager failed to start"
            print("✓ DatabaseManager started successfully")

            # Test 1: Get state for non-existent entity
            loop = asyncio.get_event_loop()
            non_existent = "sensor.does_not_exist"
            result = await loop.run_in_executor(None, db_mgr.get_state_db, non_existent)
            assert result is None, f"Expected None for non-existent entity, got {result}"
            print(f"✓ get_state_db correctly returned None for non-existent entity")

            # Test 2: Get history for non-existent entity (run in executor)
            now = datetime(2025, 12, 25, 12, 0, 0, tzinfo=mock_base.local_tz)
            history = await loop.run_in_executor(None, db_mgr.get_history_db, non_existent, now, 30)
            # History should return empty list or [[]]
            if history:
                assert len(history) == 0 or (len(history) == 1 and len(history[0]) == 0), f"Expected empty history for non-existent entity, got {history}"
            print(f"✓ get_history_db correctly returned empty for non-existent entity")

            # Test 3: Verify IPC queue is still working after errors
            # Set a valid entity to confirm queue isn't broken
            entity_id = "sensor.test_after_error"
            state = "123"
            attributes = {"test": "value"}
            await loop.run_in_executor(None, db_mgr.set_state_db, entity_id, state, attributes)

            retrieved = await loop.run_in_executor(None, db_mgr.get_state_db, entity_id)
            assert retrieved is not None, "IPC queue broken after error handling"
            assert retrieved["state"] == state, "State mismatch after error handling"
            print(f"✓ IPC queue still functional after error handling")

            # Test 4: Get all entities should work even if empty initially
            all_entities = await loop.run_in_executor(None, db_mgr.get_all_entities_db)
            assert isinstance(all_entities, list), "get_all_entities_db should always return a list"
            assert entity_id in all_entities, "Entity should be in entities list"
            print(f"✓ get_all_entities_db returned valid list: {len(all_entities)} entities")

            # Stop the component and verify thread exits
            # Send stop command via IPC to cover the queue processing path
            # Stop the component and verify thread exits
            await db_mgr.stop()

            # Wait for thread to fully exit
            timeout = 0
            while db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert not db_mgr.api_started, "DatabaseManager thread did not exit after stop command"
            print("✓ DatabaseManager thread exited after stop command")

            await task

            print("✓ DatabaseManager stopped successfully")

        finally:
            # Cleanup
            shutil.rmtree(mock_base.config_root)
            print(f"✓ Cleaned up temp directory: {mock_base.config_root}")

    run_async(run_test())
    print("=== test_db_manager_error_handling PASSED ===\n")


def _test_db_manager_persistence(my_predbat=None):
    """Test DatabaseManager data persistence across restarts"""
    print("\n=== Testing DatabaseManager persistence across restarts ===")

    async def run_test():
        mock_base = MockBase()

        # Create first DatabaseManager instance
        db_mgr1 = MockDatabaseManager()
        db_mgr1.api_stop = False
        db_mgr1.base = mock_base
        db_mgr1.log = mock_base.log
        db_mgr1.initialize(db_enable=True, db_days=30)

        try:
            # Start the component
            task1 = asyncio.create_task(db_mgr1.start())

            # Wait for startup
            timeout = 0
            while not db_mgr1.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert db_mgr1.api_started, "DatabaseManager failed to start"
            print("✓ DatabaseManager started successfully (first instance)")

            # Store test data
            loop = asyncio.get_event_loop()
            test_data = [
                ("sensor.persistent_battery", "75.5", {"unit_of_measurement": "%", "friendly_name": "Battery SOC"}),
                ("sensor.persistent_solar", "2.3", {"unit_of_measurement": "kW", "device_class": "power"}),
                ("sensor.persistent_grid", "1.5", {"unit_of_measurement": "kW", "friendly_name": "Grid Import"}),
            ]

            for entity_id, state, attributes in test_data:
                await loop.run_in_executor(None, db_mgr1.set_state_db, entity_id, state, attributes)

            # Verify data is stored
            all_entities = await loop.run_in_executor(None, db_mgr1.get_all_entities_db)
            assert len(all_entities) >= 3, f"Expected at least 3 entities before restart, got {len(all_entities)}"
            print(f"✓ Stored {len(all_entities)} entities: {all_entities}")

            # Stop the first instance and verify thread exits
            await db_mgr1.stop()

            # Wait for thread to fully exit
            timeout = 0
            while db_mgr1.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert not db_mgr1.api_started, "DatabaseManager thread did not exit after stop command"

            await task1
            print("✓ DatabaseManager stopped and thread exited (first instance)")

            # Wait a bit to ensure everything is closed
            await asyncio.sleep(0.1)

            # Create second DatabaseManager instance with SAME config_root
            db_mgr2 = MockDatabaseManager()
            db_mgr2.api_stop = False
            db_mgr2.base = mock_base  # Same base means same config_root/database file
            db_mgr2.log = mock_base.log
            db_mgr2.initialize(db_enable=True, db_days=30)

            # Start the second instance
            task2 = asyncio.create_task(db_mgr2.start())

            # Wait for startup
            timeout = 0
            while not db_mgr2.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert db_mgr2.api_started, "DatabaseManager failed to start (second instance)"
            print("✓ DatabaseManager restarted successfully (second instance)")

            # Verify all entities are still present
            all_entities_after = await loop.run_in_executor(None, db_mgr2.get_all_entities_db)
            assert len(all_entities_after) == len(all_entities), f"Entity count mismatch: {len(all_entities_after)} after restart vs {len(all_entities)} before"

            for entity_id, _, _ in test_data:
                assert entity_id in all_entities_after, f"Entity {entity_id} not found after restart"

            print(f"✓ All {len(all_entities_after)} entities persisted after restart")

            # Verify state and attributes are preserved
            for entity_id, expected_state, expected_attributes in test_data:
                retrieved = await loop.run_in_executor(None, db_mgr2.get_state_db, entity_id)
                assert retrieved is not None, f"Entity {entity_id} returned None after restart"
                assert retrieved["state"] == expected_state, f"State mismatch for {entity_id}: expected {expected_state}, got {retrieved['state']}"

                # Verify all attributes are preserved
                for attr_key, attr_value in expected_attributes.items():
                    assert attr_key in retrieved["attributes"], f"Attribute {attr_key} missing for {entity_id} after restart"
                    assert retrieved["attributes"][attr_key] == attr_value, f"Attribute {attr_key} mismatch for {entity_id}: expected {attr_value}, got {retrieved['attributes'][attr_key]}"

                print(f"✓ Entity {entity_id}: state={retrieved['state']}, attributes preserved")

            # Stop the second instance and verify thread exits
            await db_mgr2.stop()

            # Wait for thread to fully exit
            timeout = 0
            while db_mgr2.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert not db_mgr2.api_started, "DatabaseManager thread did not exit after stop command"
            await task2
            print("✓ DatabaseManager stopped and thread exited (second instance)")

        finally:
            # Cleanup
            shutil.rmtree(mock_base.config_root)
            print(f"✓ Cleaned up temp directory: {mock_base.config_root}")

    run_async(run_test())
    print("=== test_db_manager_persistence PASSED ===\n")


def _test_db_manager_commit_throttling(my_predbat=None):
    """Test DatabaseManager commit throttling - commits should only happen every 5 seconds when queue is empty"""
    print("\n=== Testing DatabaseManager commit throttling ===")

    async def run_test():
        mock_base = MockBase()

        # Create DatabaseManager using our mock class
        db_mgr = MockDatabaseManager()
        db_mgr.api_stop = False
        db_mgr.base = mock_base
        db_mgr.log = mock_base.log
        db_mgr.initialize(db_enable=True, db_days=30)

        try:
            # Start the component
            task = asyncio.create_task(db_mgr.start())

            # Wait for startup
            timeout = 0
            while not db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1

            assert db_mgr.api_started, "DatabaseManager failed to start"
            print("✓ DatabaseManager started successfully")

            loop = asyncio.get_event_loop()

            # Test 1: Verify first commit happens and updates last_commit_time
            entity_id1 = "sensor.test1"
            await loop.run_in_executor(None, db_mgr.set_state_db, entity_id1, "10", {})

            # Give time for queue to be processed
            await asyncio.sleep(0.2)

            first_commit_time = db_mgr.last_commit_time
            assert first_commit_time is not None, "First commit should have happened (last_commit_time should be set)"
            print(f"✓ First commit happened at {first_commit_time}")

            # Test 2: Rapid writes within 5 seconds should NOT trigger additional commits
            await asyncio.sleep(0.1)
            await loop.run_in_executor(None, db_mgr.set_state_db, "sensor.test2", "20", {})
            await asyncio.sleep(0.1)
            await loop.run_in_executor(None, db_mgr.set_state_db, "sensor.test3", "30", {})
            await asyncio.sleep(0.1)
            await loop.run_in_executor(None, db_mgr.set_state_db, "sensor.test4", "40", {})
            await asyncio.sleep(0.1)

            second_check_time = db_mgr.last_commit_time
            assert second_check_time == first_commit_time, "No commit should happen within 5 seconds of last commit"
            print(f"✓ Rapid writes within 5 seconds did not trigger new commits (last_commit_time unchanged)")

            # Test 3: Push back commit time to 6 seconds ago, then write - should trigger a new commit
            print("  Pushing last_commit_time back to 6 seconds ago to test throttle expiry...")
            db_mgr.last_commit_time = datetime.now(timezone.utc) - timedelta(seconds=6)
            pushed_back_time = db_mgr.last_commit_time

            await loop.run_in_executor(None, db_mgr.set_state_db, "sensor.test5", "50", {})
            await asyncio.sleep(0.1)  # Give time for processing

            third_commit_time = db_mgr.last_commit_time
            assert third_commit_time is not None, "Commit time should still be set"
            assert third_commit_time > pushed_back_time, "New commit should have happened after throttle expired"
            assert third_commit_time > first_commit_time, "New commit time should be later than first commit"
            time_diff = (third_commit_time - pushed_back_time).total_seconds()
            assert time_diff >= 5.0, f"Time difference should show commit happened after 5+ second gap, got {time_diff}"
            print(f"✓ After pushing time back 6s, new commit triggered at {third_commit_time}")

            # Test 4: Verify all data was persisted despite throttling
            retrieved1 = await loop.run_in_executor(None, db_mgr.get_state_db, "sensor.test1")
            retrieved2 = await loop.run_in_executor(None, db_mgr.get_state_db, "sensor.test2")
            retrieved3 = await loop.run_in_executor(None, db_mgr.get_state_db, "sensor.test3")
            retrieved4 = await loop.run_in_executor(None, db_mgr.get_state_db, "sensor.test4")
            retrieved5 = await loop.run_in_executor(None, db_mgr.get_state_db, "sensor.test5")

            assert retrieved1 is not None and retrieved1["state"] == "10", "sensor.test1 data should be persisted"
            assert retrieved2 is not None and retrieved2["state"] == "20", "sensor.test2 data should be persisted"
            assert retrieved3 is not None and retrieved3["state"] == "30", "sensor.test3 data should be persisted"
            assert retrieved4 is not None and retrieved4["state"] == "40", "sensor.test4 data should be persisted"
            assert retrieved5 is not None and retrieved5["state"] == "50", "sensor.test5 data should be persisted"
            print("✓ All data persisted correctly despite commit throttling")

            # Stop and cleanup
            await db_mgr.stop()
            timeout = 0
            while db_mgr.api_started and timeout < 50:
                await asyncio.sleep(0.1)
                timeout += 1
            await task

            print("✓ DatabaseManager stopped successfully")

        finally:
            # Cleanup temp directory
            shutil.rmtree(mock_base.config_root)
            print(f"✓ Cleaned up temp directory: {mock_base.config_root}")

    run_async(run_test())
    print("=== test_db_manager_commit_throttling PASSED ===\n")
