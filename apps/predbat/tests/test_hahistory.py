# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import pytz
from datetime import datetime, timedelta
from ha import HAHistory
from utils import str2time
from tests.test_infra import run_async


FIVE_MINUTE_ENTRIES_PER_DAY = 1440 // 5


class MockComponents:
    """Mock components registry"""

    def __init__(self):
        self.components = {}

    def get_component(self, name):
        return self.components.get(name, None)

    def register_component(self, name, component):
        self.components[name] = component


class MockHAInterface:
    """Mock HAInterface for testing HAHistory"""

    def __init__(self):
        self.history_data = {}  # entity_id -> list of history entries
        self.get_history_calls = []  # Track calls for verification

    def get_history(self, entity_id, now, days=30, from_time=None):
        """Mock get_history method"""
        self.get_history_calls.append({"entity_id": entity_id, "now": now, "days": days, "from_time": from_time})

        # Return mock history data if available
        if entity_id in self.history_data:
            history = self.history_data[entity_id]
            # Filter by from_time if provided
            if from_time:
                history = [entry for entry in history if str2time(entry.get("last_updated", "")) > from_time]
            # Else filter by days
            else:
                num_entries = days * FIVE_MINUTE_ENTRIES_PER_DAY
                history = history[-num_entries:]
            return [history] if history else None
        return None

    def add_mock_history(self, entity_id, history_list):
        """Add mock history data for an entity"""
        self.history_data[entity_id] = history_list


class MockBase:
    """Mock base class for HAHistory testing"""

    def __init__(self):
        self.components = MockComponents()
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.last_success_timestamp = None
        self.prefix = "predbat"
        self.args = {}

    def log(self, message):
        """Log messages for test verification"""
        self.log_messages.append(message)

    def call_notify(self, message):
        """Mock notify method"""
        self.log_messages.append("Alert: " + message)


def create_mock_history(entity_id, days=30, step_minutes=5, start_time=None):
    """Create realistic history data for an entity"""
    history = []
    if start_time is None:
        start_time = datetime.now(pytz.UTC) - timedelta(days=days)

    total_entries = int(days * 24 * 60 / step_minutes)
    for count in range(total_entries):
        point = start_time + timedelta(minutes=count * step_minutes)
        history.append(
            {
                "state": str(count * 0.1),
                "last_updated": point.strftime("%Y-%m-%dT%H:%M:%S.%f%z"),
                "attributes": {"unit_of_measurement": "kWh", "friendly_name": f"Test {entity_id}", "device_class": "energy"},
            }
        )

    return history


def test_hahistory_initialize(my_predbat=None):
    """Test HAHistory initialization"""
    print("\n=== Testing HAHistory initialize() ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    # Verify initialization
    if not isinstance(ha_history.history_entities, dict):
        print("ERROR: history_entities should be a dict")
        failed += 1
    elif len(ha_history.history_entities) != 0:
        print("ERROR: history_entities should be empty after initialization")
        failed += 1
    else:
        print("✓ history_entities initialized correctly")

    if not isinstance(ha_history.history_data, dict):
        print("ERROR: history_data should be a dict")
        failed += 1
    elif len(ha_history.history_data) != 0:
        print("ERROR: history_data should be empty after initialization")
        failed += 1
    else:
        print("✓ history_data initialized correctly")

    return failed


def test_hahistory_add_entity(my_predbat=None):
    """Test HAHistory add_entity() method"""
    print("\n=== Testing HAHistory add_entity() ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    # Test adding new entity
    ha_history.add_entity("sensor.battery", 30)
    if ha_history.history_entities.get("sensor.battery") != 30:
        print("ERROR: Failed to add new entity with 30 days")
        failed += 1
    else:
        print("✓ Added new entity with 30 days")

    # Test updating entity with fewer days (should not update)
    ha_history.add_entity("sensor.battery", 7)
    if ha_history.history_entities.get("sensor.battery") != 30:
        print("ERROR: Should not update entity to fewer days")
        failed += 1
    else:
        print("✓ Correctly kept maximum days (30)")

    # Test updating entity with more days (should update)
    ha_history.add_entity("sensor.battery", 60)
    if ha_history.history_entities.get("sensor.battery") != 60:
        print("ERROR: Failed to update entity to more days")
        failed += 1
    else:
        print("✓ Updated entity to more days (60)")

    # Test adding multiple entities
    ha_history.add_entity("sensor.solar", 14)
    ha_history.add_entity("sensor.grid", 7)
    if len(ha_history.history_entities) != 3:
        print("ERROR: Should have 3 entities tracked")
        failed += 1
    else:
        print("✓ Multiple entities tracked correctly")

    return failed


def test_hahistory_get_history_no_interface(my_predbat=None):
    """Test HAHistory get_history() when no HAInterface available"""
    print("\n=== Testing HAHistory get_history() with no HAInterface ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    # Try to get history without HAInterface registered
    result = ha_history.get_history("sensor.battery", days=30)

    if result is not None:
        print("ERROR: Should return None when no HAInterface")
        failed += 1
    else:
        print("✓ Returned None when no HAInterface")

    # Check for error log
    error_found = any("No HAInterface available" in msg for msg in mock_base.log_messages)
    if not error_found:
        print("ERROR: Should log error when no HAInterface")
        failed += 1
    else:
        print("✓ Logged error when no HAInterface")

    return failed


def test_hahistory_get_history_fetch_and_cache(my_predbat=None):
    """Test HAHistory get_history() fetching from HAInterface and caching"""
    print("\n=== Testing HAHistory get_history() fetch and cache ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    # Setup mock HAInterface
    mock_ha = MockHAInterface()
    mock_base.components.register_component("ha", mock_ha)

    # Create mock history data
    entity_id = "sensor.battery"
    mock_history = create_mock_history(entity_id, days=60)
    mock_ha.add_mock_history(entity_id, mock_history)

    length_30_days_history = 30 * FIVE_MINUTE_ENTRIES_PER_DAY
    length_60_days_history = 60 * FIVE_MINUTE_ENTRIES_PER_DAY

    # Test 1: Fetch history (tracked=True, should cache)
    result = ha_history.get_history(entity_id, days=30, tracked=True)

    if result is None:
        print("ERROR: Should return history data")
        failed += 1
    elif len(result) != 1:
        print("ERROR: Should return list with one element")
        failed += 1
    elif len(result[0]) != length_30_days_history:
        print(f"ERROR: Expected {len(mock_history)} entries, got {len(result[0])}")
        failed += 1
    else:
        print(f"✓ Fetched history with {len(result[0])} entries")

    # Verify entity was tracked
    if entity_id not in ha_history.history_entities:
        print("ERROR: Entity should be tracked")
        failed += 1
    elif ha_history.history_entities[entity_id] != 30:
        print("ERROR: Entity should be tracked with 30 days")
        failed += 1
    else:
        print("✓ Entity tracked correctly")

    # Verify data was cached
    if entity_id not in ha_history.history_data:
        print("ERROR: History should be cached")
        failed += 1
    else:
        print("✓ History cached correctly")

    # Test 2: Get from cache (should not call HAInterface again)
    initial_call_count = len(mock_ha.get_history_calls)
    result2 = ha_history.get_history(entity_id, days=30, tracked=True)

    if len(mock_ha.get_history_calls) != initial_call_count:
        print("ERROR: Should use cache, not call HAInterface again")
        failed += 1
    else:
        print("✓ Used cache instead of fetching again")

    # Test 3: Request more days (should fetch again)
    result3 = ha_history.get_history(entity_id, days=60, tracked=True)
    if len(mock_ha.get_history_calls) == initial_call_count:
        print("ERROR: Should fetch when requesting more days")
        failed += 1
    else:
        print("✓ Fetched when requesting more days")

    # Verify entity tracking was updated
    if ha_history.history_entities[entity_id] != 60:
        print("ERROR: Entity tracking should be updated to 60 days")
        failed += 1
    else:
        print("✓ Entity tracking updated to 60 days")

    # Test 4: Request more days again - cache should have them populated
    result4 = ha_history.get_history(entity_id, days=60, tracked=True)
    if len(result4[0]) != length_60_days_history:
        print("ERROR: Cache did not correctly return 60 days")
        failed += 1
    else:
        print("✓ Cache correctly populated with longer history")

    sorted_result4 = sorted(result4[0], key=lambda x: x["last_updated"])
    if sorted_result4 != result4[0]:
        print("ERROR: Cache population did not correctly order entries when adding longer history")
        failed += 1
    else:
        print("✓ Cache order correct after adding longer history")

    return failed


def test_hahistory_get_history_untracked(my_predbat=None):
    """Test HAHistory get_history() with tracked=False"""
    print("\n=== Testing HAHistory get_history() untracked ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    # Setup mock HAInterface
    mock_ha = MockHAInterface()
    mock_base.components.register_component("ha", mock_ha)

    entity_id = "sensor.solar"
    mock_history = create_mock_history(entity_id, days=7)
    mock_ha.add_mock_history(entity_id, mock_history)

    # Fetch with tracked=False
    result = ha_history.get_history(entity_id, days=7, tracked=False)

    if result is None:
        print("ERROR: Should return history data")
        failed += 1
    else:
        print("✓ Fetched untracked history")

    # Verify entity was NOT tracked
    if entity_id in ha_history.history_entities:
        print("ERROR: Entity should not be tracked when tracked=False")
        failed += 1
    else:
        print("✓ Entity not tracked correctly")

    # Verify data was NOT cached
    if entity_id in ha_history.history_data:
        print("ERROR: History should not be cached when tracked=False")
        failed += 1
    else:
        print("✓ History not cached correctly")

    return failed


def test_hahistory_expand_from_7_to_30_days(my_predbat=None):
    """Test HAHistory get_history() correctly fetches full 30 days when only 7 days cached"""
    print("\n=== Testing HAHistory get_history() expand from 7 to 30 days ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    # Setup mock HAInterface
    mock_ha = MockHAInterface()
    mock_base.components.register_component("ha", mock_ha)

    # Create 30 days of mock history data available in the "backend"
    entity_id = "sensor.battery"
    mock_history_30_days = create_mock_history(entity_id, days=30)
    mock_ha.add_mock_history(entity_id, mock_history_30_days)

    length_7_days = 7 * FIVE_MINUTE_ENTRIES_PER_DAY
    length_30_days = 30 * FIVE_MINUTE_ENTRIES_PER_DAY

    # Step 1: First fetch only 7 days (simulate initial cache)
    print("\n  Step 1: Fetching 7 days to populate cache...")
    result_7 = ha_history.get_history(entity_id, days=7, tracked=True)

    if result_7 is None:
        print("ERROR: Should return 7 days of history")
        failed += 1
    elif len(result_7[0]) != length_7_days:
        print(f"ERROR: Expected {length_7_days} entries for 7 days, got {len(result_7[0])}")
        failed += 1
    else:
        print(f"✓ Fetched and cached 7 days ({len(result_7[0])} entries)")

    # Verify entity is tracked with 7 days
    if ha_history.history_entities.get(entity_id) != 7:
        print(f"ERROR: Entity should be tracked with 7 days, but has {ha_history.history_entities.get(entity_id)}")
        failed += 1
    else:
        print("✓ Entity tracked with 7 days")

    # Step 2: Now request 30 days (more than cached)
    print("\n  Step 2: Requesting 30 days (more than cached 7 days)...")
    initial_call_count = len(mock_ha.get_history_calls)
    result_30 = ha_history.get_history(entity_id, days=30, tracked=True)

    # Verify a new fetch occurred
    if len(mock_ha.get_history_calls) == initial_call_count:
        print("ERROR: Should fetch from HAInterface when requesting more days than cached")
        failed += 1
    else:
        print("✓ Correctly triggered new fetch from HAInterface")

    # Verify we got the FULL 30 days, not just 7
    if result_30 is None:
        print("ERROR: Should return 30 days of history")
        failed += 1
    elif len(result_30[0]) != length_30_days:
        print(f"ERROR: Expected {length_30_days} entries for 30 days, got {len(result_30[0])}")
        print(f"       This suggests only {len(result_30[0]) / FIVE_MINUTE_ENTRIES_PER_DAY:.1f} days were returned")
        failed += 1
    else:
        print(f"✓ Correctly returned FULL 30 days of history ({len(result_30[0])} entries)")

    # Verify entity tracking was updated to 30 days
    if ha_history.history_entities.get(entity_id) != 30:
        print(f"ERROR: Entity should be tracked with 30 days, but has {ha_history.history_entities.get(entity_id)}")
        failed += 1
    else:
        print("✓ Entity tracking updated to 30 days")

    # Verify the last fetch call requested 30 days
    last_call = mock_ha.get_history_calls[-1]
    if last_call["days"] != 30:
        print(f"ERROR: Last fetch should have requested 30 days, but requested {last_call['days']}")
        failed += 1
    else:
        print("✓ Fetch correctly requested 30 days from backend")

    # Step 3: Request 30 days again - should use cache now
    print("\n  Step 3: Requesting 30 days again (should use cache)...")
    cache_call_count = len(mock_ha.get_history_calls)
    result_30_again = ha_history.get_history(entity_id, days=30, tracked=True)

    if len(mock_ha.get_history_calls) != cache_call_count:
        print("ERROR: Should use cache when requesting same or fewer days")
        failed += 1
    else:
        print("✓ Correctly used cache for subsequent request")

    if len(result_30_again[0]) != length_30_days:
        print(f"ERROR: Cached result should still have {length_30_days} entries, got {len(result_30_again[0])}")
        failed += 1
    else:
        print("✓ Cache correctly returns full 30 days")

    return failed


def test_hahistory_update_entity_filter_attributes(my_predbat=None):
    """Test HAHistory update_entity() filters unwanted attributes"""
    print("\n=== Testing HAHistory update_entity() attribute filtering ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    entity_id = "sensor.battery"

    # Create history with attributes that should be filtered
    new_history = [
        {
            "state": "42.5",
            "last_updated": "2025-12-25T10:00:00.000000+00:00",
            "last_changed": "2025-12-25T10:00:00.000000+00:00",  # Should be filtered
            "entity_id": entity_id,  # Should be filtered
            "attributes": {
                "friendly_name": "Battery",  # Should be filtered
                "unit_of_measurement": "kWh",  # Should be filtered
                "icon": "mdi:battery",  # Should be filtered
                "device_class": "energy",  # Should be filtered
                "state_class": "measurement",  # Should be filtered
                "custom_attr": "keep_this",  # Should be kept
            },
        }
    ]

    ha_history.update_entity(entity_id, new_history)

    # Verify filtering
    if entity_id not in ha_history.history_data:
        print("ERROR: History should be stored")
        failed += 1
    else:
        entry = ha_history.history_data[entity_id][0]

        # Check filtered entry fields
        if "last_changed" in entry:
            print("ERROR: last_changed should be filtered")
            failed += 1
        if "entity_id" in entry:
            print("ERROR: entity_id should be filtered from entry")
            failed += 1

        # Check filtered attributes
        attrs = entry.get("attributes", {})
        if "friendly_name" in attrs:
            print("ERROR: friendly_name should be filtered")
            failed += 1
        if "unit_of_measurement" in attrs:
            print("ERROR: unit_of_measurement should be filtered")
            failed += 1
        if "icon" in attrs:
            print("ERROR: icon should be filtered")
            failed += 1
        if "device_class" in attrs:
            print("ERROR: device_class should be filtered")
            failed += 1
        if "state_class" in attrs:
            print("ERROR: state_class should be filtered")
            failed += 1

        # Check kept attributes
        if "custom_attr" not in attrs:
            print("ERROR: custom_attr should be kept")
            failed += 1
        elif attrs["custom_attr"] != "keep_this":
            print("ERROR: custom_attr value incorrect")
            failed += 1

        if failed == 0:
            print("✓ Attributes filtered correctly")

    return failed


def test_hahistory_update_entity_merge_new(my_predbat=None):
    """Test HAHistory update_entity() merges new entries"""
    print("\n=== Testing HAHistory update_entity() merge logic ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    entity_id = "sensor.battery"
    base_time = datetime(2025, 12, 25, 10, 0, 0, tzinfo=pytz.UTC)

    # Initial history
    initial_history = [
        {"state": "10", "last_updated": (base_time + timedelta(minutes=0)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"), "attributes": {}},
        {"state": "20", "last_updated": (base_time + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"), "attributes": {}},
        {"state": "30", "last_updated": (base_time + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"), "attributes": {}},
    ]

    ha_history.update_entity(entity_id, initial_history)

    initial_count = len(ha_history.history_data[entity_id])
    if initial_count != 3:
        print(f"ERROR: Should have 3 initial entries, got {initial_count}")
        failed += 1
    else:
        print("✓ Initial history loaded correctly")

    # New history with overlapping and new entries
    new_history = [
        {"state": "30", "last_updated": (base_time + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"), "attributes": {}},  # Duplicate
        {"state": "40", "last_updated": (base_time + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"), "attributes": {}},  # New
        {"state": "50", "last_updated": (base_time + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"), "attributes": {}},  # New
    ]

    ha_history.update_entity(entity_id, new_history)

    final_count = len(ha_history.history_data[entity_id])
    if final_count != 5:
        print(f"ERROR: Should have 5 entries after merge (3 old + 2 new), got {final_count}")
        failed += 1
    else:
        print("✓ New entries merged correctly")

    # Verify ordering (oldest to newest)
    states = [entry["state"] for entry in ha_history.history_data[entity_id]]
    expected_states = ["10", "20", "30", "40", "50"]
    if states != expected_states:
        print(f"ERROR: Expected states {expected_states}, got {states}")
        failed += 1
    else:
        print("✓ History maintained correct order")

    return failed


def test_hahistory_prune_history(my_predbat=None):
    """Test HAHistory prune_history() removes old entries"""
    print("\n=== Testing HAHistory prune_history() ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    entity_id = "sensor.battery"
    now = datetime(2025, 12, 25, 10, 0, 0, tzinfo=pytz.UTC)

    # Create history spanning 60 days
    history = create_mock_history(entity_id, days=60, step_minutes=60, start_time=now - timedelta(days=60))
    ha_history.history_data[entity_id] = history
    ha_history.history_entities[entity_id] = 30  # Only keep 30 days

    initial_count = len(ha_history.history_data[entity_id])
    print(f"  Initial history entries: {initial_count}")

    # Prune to 30 days
    ha_history.prune_history(now)

    final_count = len(ha_history.history_data[entity_id])
    print(f"  After pruning: {final_count} entries")

    # Verify entries are within 30 days
    cutoff_time = now - timedelta(days=30)
    for entry in ha_history.history_data[entity_id]:
        entry_time = str2time(entry["last_updated"])
        if entry_time < cutoff_time:
            print(f"ERROR: Found entry older than cutoff: {entry['last_updated']}")
            failed += 1
            break

    if failed == 0:
        print("✓ All entries within 30-day window")

    # Verify some entries were removed
    if final_count >= initial_count:
        print("ERROR: Expected entries to be removed")
        failed += 1
    else:
        print(f"✓ Pruned {initial_count - final_count} old entries")

    return failed


def test_hahistory_prune_empty_history(my_predbat=None):
    """Test HAHistory prune_history() with empty history"""
    print("\n=== Testing HAHistory prune_history() with empty history ===")
    failed = 0

    mock_base = MockBase()
    ha_history = HAHistory(mock_base)
    ha_history.initialize()

    entity_id = "sensor.battery"
    ha_history.history_data[entity_id] = []
    ha_history.history_entities[entity_id] = 30

    now = datetime.now(pytz.UTC)

    # Should not crash with empty history
    try:
        ha_history.prune_history(now)
        print("✓ Handled empty history without error")
    except Exception as e:
        print(f"ERROR: Exception with empty history: {e}")
        failed += 1

    return failed


def test_hahistory_run_first_call(my_predbat=None):
    """Test HAHistory run() method on first call"""
    print("\n=== Testing HAHistory run() first call ===")
    failed = 0

    async def run_test():
        mock_base = MockBase()
        ha_history = HAHistory(mock_base)
        ha_history.initialize()

        # Setup mock HAInterface
        mock_ha = MockHAInterface()
        mock_base.components.register_component("ha", mock_ha)

        # Add tracked entity
        entity_id = "sensor.battery"
        ha_history.add_entity(entity_id, 30)
        mock_history = create_mock_history(entity_id, days=30)
        mock_ha.add_mock_history(entity_id, mock_history)

        # Run with first=True
        result = await ha_history.run(seconds=0, first=True)

        if not result:
            print("ERROR: run() should return True on success")
            return 1

        # Verify startup log
        startup_log = any("Starting HAHistory" in msg for msg in mock_base.log_messages)
        if not startup_log:
            print("ERROR: Should log startup message")
            return 1
        else:
            print("✓ Logged startup message")

        # Verify history was fetched for tracked entity
        if len(mock_ha.get_history_calls) == 0:
            print("ERROR: Should fetch history on first run")
            return 1
        else:
            print(f"✓ Fetched history for {len(mock_ha.get_history_calls)} entity(ies)")

        # Verify history was cached
        if entity_id not in ha_history.history_data:
            print("ERROR: History should be cached")
            return 1
        else:
            print("✓ History cached correctly")

        return 0

    failed = run_async(run_test())
    return failed


def test_hahistory_run_no_ha_interface(my_predbat=None):
    """Test HAHistory run() returns False when no HAInterface"""
    print("\n=== Testing HAHistory run() with no HAInterface ===")
    failed = 0

    async def run_test():
        mock_base = MockBase()
        ha_history = HAHistory(mock_base)
        ha_history.initialize()

        # Run without HAInterface
        result = await ha_history.run(seconds=0, first=True)

        if result:
            print("ERROR: run() should return False when no HAInterface")
            return 1

        # Verify error log
        error_log = any("No HAInterface available" in msg for msg in mock_base.log_messages)
        if not error_log:
            print("ERROR: Should log error when no HAInterface")
            return 1
        else:
            print("✓ Returned False and logged error")

        return 0

    failed = run_async(run_test())
    return failed


def test_hahistory_run_periodic_update(my_predbat=None):
    """Test HAHistory run() periodic updates (2 minutes)"""
    print("\n=== Testing HAHistory run() periodic updates ===")
    failed = 0

    async def run_test():
        mock_base = MockBase()
        ha_history = HAHistory(mock_base)
        ha_history.initialize()

        # Setup mock HAInterface
        mock_ha = MockHAInterface()
        mock_base.components.register_component("ha", mock_ha)

        # Add tracked entity with existing history
        entity_id = "sensor.battery"
        base_time = datetime(2025, 12, 25, 10, 0, 0, tzinfo=pytz.UTC)
        initial_history = create_mock_history(entity_id, days=1, step_minutes=5, start_time=base_time - timedelta(days=1))

        ha_history.add_entity(entity_id, 30)
        ha_history.history_data[entity_id] = initial_history

        # Add new history to mock interface (simulating new data)
        new_history = create_mock_history(entity_id, days=1, step_minutes=5, start_time=base_time)
        mock_ha.add_mock_history(entity_id, new_history)

        # Run at 120 seconds (2 minutes) - should trigger update
        initial_call_count = len(mock_ha.get_history_calls)
        result = await ha_history.run(seconds=120, first=False)

        if not result:
            print("ERROR: run() should return True")
            return 1

        # Verify history was fetched
        if len(mock_ha.get_history_calls) <= initial_call_count:
            print("ERROR: Should fetch history at 2-minute interval")
            return 1
        else:
            print("✓ Fetched history at 2-minute interval")

        # Verify incremental fetch (with from_time)
        last_call = mock_ha.get_history_calls[-1]
        if last_call["from_time"] is None:
            print("ERROR: Should use from_time for incremental fetch")
            return 1
        else:
            print("✓ Used incremental fetch with from_time")

        return 0

    failed = run_async(run_test())
    return failed


def test_hahistory_run_hourly_prune(my_predbat=None):
    """Test HAHistory run() hourly pruning"""
    print("\n=== Testing HAHistory run() hourly pruning ===")
    failed = 0

    async def run_test():
        mock_base = MockBase()
        ha_history = HAHistory(mock_base)
        ha_history.initialize()

        # Setup mock HAInterface
        mock_ha = MockHAInterface()
        mock_base.components.register_component("ha", mock_ha)

        # Add entity with old history
        entity_id = "sensor.battery"
        now = datetime.now(pytz.UTC)
        history = create_mock_history(entity_id, days=60, step_minutes=60, start_time=now - timedelta(days=60))

        ha_history.add_entity(entity_id, 30)
        ha_history.history_data[entity_id] = history

        initial_count = len(ha_history.history_data[entity_id])

        # Run at 3600 seconds (1 hour) - should trigger prune
        result = await ha_history.run(seconds=3600, first=False)

        if not result:
            print("ERROR: run() should return True")
            return 1

        # Verify pruning log
        prune_log = any("Pruning history data" in msg for msg in mock_base.log_messages)
        if not prune_log:
            print("ERROR: Should log pruning message")
            return 1
        else:
            print("✓ Logged pruning message")

        # Verify entries were pruned
        final_count = len(ha_history.history_data[entity_id])
        if final_count >= initial_count:
            print("ERROR: Expected entries to be pruned")
            return 1
        else:
            print(f"✓ Pruned {initial_count - final_count} entries")

        return 0

    failed = run_async(run_test())
    return failed


def test_hahistory_run_no_update_timing(my_predbat=None):
    """Test HAHistory run() doesn't update at wrong timing"""
    print("\n=== Testing HAHistory run() timing logic ===")
    failed = 0

    async def run_test():
        mock_base = MockBase()
        ha_history = HAHistory(mock_base)
        ha_history.initialize()

        # Setup mock HAInterface
        mock_ha = MockHAInterface()
        mock_base.components.register_component("ha", mock_ha)

        # Add tracked entity
        entity_id = "sensor.battery"
        ha_history.add_entity(entity_id, 30)

        # Run at 60 seconds (not a 2-minute or 1-hour mark)
        result = await ha_history.run(seconds=60, first=False)

        if not result:
            print("ERROR: run() should return True")
            return 1

        # Verify NO history fetch occurred
        if len(mock_ha.get_history_calls) > 0:
            print("ERROR: Should not fetch history at 60 seconds")
            return 1
        else:
            print("✓ Correctly skipped update at non-trigger time")

        return 0

    failed = run_async(run_test())
    return failed


def run_hahistory_tests(my_predbat):
    """Run all HAHistory unit tests"""
    print("\n" + "=" * 80)
    print("HAHistory Unit Tests")
    print("=" * 80)

    failed = 0

    # Basic functionality tests
    failed += test_hahistory_initialize(my_predbat)
    failed += test_hahistory_add_entity(my_predbat)

    # get_history tests
    failed += test_hahistory_get_history_no_interface(my_predbat)
    failed += test_hahistory_get_history_fetch_and_cache(my_predbat)
    failed += test_hahistory_get_history_untracked(my_predbat)
    failed += test_hahistory_expand_from_7_to_30_days(my_predbat)

    # Data management tests
    failed += test_hahistory_update_entity_filter_attributes(my_predbat)
    failed += test_hahistory_update_entity_merge_new(my_predbat)
    failed += test_hahistory_prune_history(my_predbat)
    failed += test_hahistory_prune_empty_history(my_predbat)

    # Async run tests
    failed += test_hahistory_run_first_call(my_predbat)
    failed += test_hahistory_run_no_ha_interface(my_predbat)
    failed += test_hahistory_run_periodic_update(my_predbat)
    failed += test_hahistory_run_hourly_prune(my_predbat)
    failed += test_hahistory_run_no_update_timing(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAHistory tests passed!")
    else:
        print(f"❌ {failed} HAHistory test(s) failed")
    print("=" * 80 + "\n")

    return failed
