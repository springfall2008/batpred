# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import base64
import json
import pytz
import zlib
from datetime import datetime, timedelta
from ha import HAHistory, EntityHistory
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

    # Verify initialisation
    if not isinstance(ha_history.history_entities, dict):
        print("ERROR: history_entities should be a dict")
        failed += 1
    elif len(ha_history.history_entities) != 0:
        print("ERROR: history_entities should be empty after initialisation")
        failed += 1
    else:
        print("✓ history_entities initialised correctly")

    if not isinstance(ha_history.history_data, dict):
        print("ERROR: history_data should be a dict")
        failed += 1
    elif len(ha_history.history_data) != 0:
        print("ERROR: history_data should be empty after initialisation")
        failed += 1
    else:
        print("✓ history_data initialised correctly")

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
    ha_history.get_history(entity_id, days=30, tracked=True)

    if len(mock_ha.get_history_calls) != initial_call_count:
        print("ERROR: Should use cache, not call HAInterface again")
        failed += 1
    else:
        print("✓ Used cache instead of fetching again")

    # Test 3: Request more days (should fetch again)
    ha_history.get_history(entity_id, days=60, tracked=True)
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

    # Verify ordering (oldest to newest) - numeric states come back as floats from the columnar store
    states = [entry["state"] for entry in ha_history.history_data[entity_id]]
    expected_states = [10.0, 20.0, 30.0, 40.0, 50.0]
    if states != expected_states:
        print(f"ERROR: Expected states {expected_states}, got {states}")
        failed += 1
    else:
        print("✓ History maintained correct order")

    return failed


def test_hahistory_merge_drops_restamped_anchor(my_predbat=None):
    """Test a more-days merge replaces the record prune re-stamped at the cutoff with the fetched data"""
    print("\n=== Testing HAHistory update_entity() merge over a re-stamped prune anchor ===")
    failed = 0

    entity_id = "sensor.battery"
    base_time = datetime(2025, 12, 25, 10, 0, 0, tzinfo=pytz.UTC)
    cutoff = base_time + timedelta(days=2)

    def record(state, offset):
        """Build a history record stamped offset after base_time"""
        return {"state": state, "last_updated": (base_time + offset).isoformat(), "attributes": {}}

    # The cache missed the change to 5 a day in (a failed chunk), so prune re-stamps 7 at the cutoff,
    # and the refetch must not leave 7 asserted at the cutoff after the 5 it supplies
    full_history = [record("7", timedelta(0)), record("5", timedelta(days=1)), record("9", timedelta(days=3))]
    for label, cached, expected in [
        ("a cache that missed a change", [full_history[0], full_history[2]], [7.0, 5.0, 9.0]),
        ("a complete cache", full_history, [7.0, 5.0, 9.0]),
    ]:
        ha_history = HAHistory(MockBase())
        ha_history.initialize()
        ha_history.update_entity(entity_id, [dict(entry) for entry in cached])
        ha_history.history_data[entity_id].prune(cutoff.timestamp())
        ha_history.update_entity(entity_id, [dict(entry) for entry in full_history])
        states = [entry["state"] for entry in ha_history.history_data[entity_id]]
        if states != expected:
            print(f"ERROR: Merge over {label}: expected states {expected}, got {states}")
            failed += 1
        else:
            print(f"✓ Merge over {label} drops the re-stamped anchor")

    # A real record exactly on the cutoff is not a re-stamped anchor, so the merge keeps it
    ha_history = HAHistory(MockBase())
    ha_history.initialize()
    on_cutoff = [record("7", timedelta(0)), record("8", timedelta(days=2)), record("9", timedelta(days=3))]
    ha_history.update_entity(entity_id, [dict(entry) for entry in on_cutoff])
    ha_history.history_data[entity_id].prune(cutoff.timestamp())
    ha_history.update_entity(entity_id, [dict(entry) for entry in on_cutoff])
    states = [entry["state"] for entry in ha_history.history_data[entity_id]]
    if states != [7.0, 8.0, 9.0]:
        print(f"ERROR: Merge over a record on the cutoff: expected [7.0, 8.0, 9.0], got {states}")
        failed += 1
    else:
        print("✓ Merge keeps a real record that lands on the cutoff")

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

    # Create history spanning 60 days, offset so the cutoff falls between two records (#5456)
    history = create_mock_history(entity_id, days=60, step_minutes=60, start_time=now - timedelta(days=60, minutes=-7))
    ha_history.history_data[entity_id] = EntityHistory.from_records(history)
    ha_history.history_entities[entity_id] = 30  # Only keep 30 days

    initial_count = len(ha_history.history_data[entity_id])
    print(f"  Initial history entries: {initial_count}")

    # Prune to 30 days
    ha_history.prune_history(now)

    final_count = len(ha_history.history_data[entity_id])
    print(f"  After pruning: {final_count} entries")

    # Verify entries are within 30 days; the first holds the state in force at the cutoff, stamped there (#5456)
    cutoff_time = now - timedelta(days=30)
    for entry in ha_history.history_data[entity_id]:
        entry_time = str2time(entry["last_updated"])
        if entry_time < cutoff_time:
            print(f"ERROR: Found entry older than cutoff: {entry['last_updated']}")
            failed += 1
            break

    if failed == 0:
        print("✓ All entries within 30-day window")

    first_time = str2time(ha_history.history_data[entity_id][0]["last_updated"])
    if first_time != cutoff_time:
        print(f"ERROR: Expected the first entry at the cutoff {cutoff_time}, got {first_time}")
        failed += 1

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
    ha_history.history_data[entity_id] = EntityHistory()
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
        ha_history.history_data[entity_id] = EntityHistory.from_records(initial_history)

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
        ha_history.history_data[entity_id] = EntityHistory.from_records(history)

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


def test_entity_history_store(my_predbat=None):
    """Test the EntityHistory columnar store round-trip, exceptions, indexing and pruning"""
    print("\n=== Testing EntityHistory columnar store ===")
    failed = 0

    base_time = datetime(2025, 12, 25, 10, 0, 0, tzinfo=pytz.UTC)
    records = [
        {"state": "10.5", "last_updated": (base_time + timedelta(minutes=0)).isoformat(), "attributes": {}},
        {"state": "unknown", "last_updated": (base_time + timedelta(minutes=5)).isoformat(), "attributes": {}},
        {"state": "20.25", "last_updated": (base_time + timedelta(minutes=10)).isoformat(), "attributes": {"special": True}},
        {"state": "Charging", "last_updated": (base_time + timedelta(minutes=15)).isoformat(), "attributes": {}},
        {"state": "30", "last_updated": (base_time + timedelta(minutes=20)).isoformat(), "attributes": {}},
    ]

    store = EntityHistory.from_records(records)

    # Test 1: length and iteration
    if len(store) != 5:
        print(f"ERROR: Expected 5 records in store, got {len(store)}")
        failed += 1
    else:
        print("✓ Store holds all records")

    # Test 2: round-trip states - numeric become floats, non-numeric preserved exactly
    output = store.to_records()
    states = [entry["state"] for entry in output]
    expected = [10.5, "unknown", 20.25, "Charging", 30.0]
    if states != expected:
        print(f"ERROR: Expected states {expected}, got {states}")
        failed += 1
    else:
        print("✓ States round-trip correctly (numeric as float, non-numeric preserved)")

    # Test 3: round-trip timestamps parse back to the same instant
    times_in = [str2time(entry["last_updated"]) for entry in records]
    times_out = [str2time(entry["last_updated"]) for entry in output]
    if times_in != times_out:
        print(f"ERROR: Timestamps did not round-trip, in {times_in} out {times_out}")
        failed += 1
    else:
        print("✓ Timestamps round-trip to the same instant")

    # Test 4: attribute exceptions are preserved, shared attributes for the rest
    if output[2]["attributes"] != {"special": True}:
        print(f"ERROR: Expected exception attributes for record 2, got {output[2]['attributes']}")
        failed += 1
    elif output[0]["attributes"] != {} or output[4]["attributes"] != {}:
        print(f"ERROR: Expected empty shared attributes, got {output[0]['attributes']} and {output[4]['attributes']}")
        failed += 1
    else:
        print("✓ Attribute exceptions preserved, shared attributes returned for the rest")

    # Test 5: mutation of materialised records does not corrupt the store
    output[0]["state"] = "corrupted"
    output[0]["attributes"]["injected"] = True
    fresh = store.to_records()
    if fresh[0]["state"] != 10.5 or "injected" in fresh[0]["attributes"]:
        print(f"ERROR: Store was corrupted by mutating materialised records: {fresh[0]}")
        failed += 1
    else:
        print("✓ Materialised records are safe to mutate")

    # Test 6: indexing (including negative) and iteration match to_records
    if store[0]["state"] != 10.5 or store[-1]["state"] != 30.0 or [entry["state"] for entry in store] != expected:
        print(f"ERROR: Indexing/iteration mismatch: first {store[0]} last {store[-1]}")
        failed += 1
    else:
        print("✓ Indexing and iteration materialise records correctly")

    # Test 7: prune drops older records and their exceptions, but keeps the state in force at the
    # cutoff (the 10-minute record, with its attributes) re-stamped at the cutoff (#5456)
    cutoff_epoch = (base_time + timedelta(minutes=12)).timestamp()
    store.prune(cutoff_epoch)
    remaining = store.to_records()
    if [entry["state"] for entry in remaining] != [20.25, "Charging", 30.0]:
        print(f"ERROR: Expected [20.25, 'Charging', 30.0] after prune, got {[entry['state'] for entry in remaining]}")
        failed += 1
    elif str2time(remaining[0]["last_updated"]).timestamp() != cutoff_epoch:
        print(f"ERROR: Expected the kept record re-stamped at the cutoff, got {remaining[0]['last_updated']}")
        failed += 1
    elif remaining[0]["attributes"] != {"special": True}:
        print(f"ERROR: Expected the kept record to keep its attributes, got {remaining[0]['attributes']}")
        failed += 1
    elif store.state_exceptions and min(store.state_exceptions) < cutoff_epoch:
        print(f"ERROR: Prune left stale state exceptions: {store.state_exceptions}")
        failed += 1
    elif list(store.attribute_exceptions) != [cutoff_epoch]:
        print(f"ERROR: Expected only the kept record's attribute exception, got {store.attribute_exceptions}")
        failed += 1
    else:
        print("✓ Prune drops old records and stale exceptions, keeping the state in force at the cutoff")

    # Test 7b: a record exactly on the cutoff is the state in force there, so nothing older is kept
    store.prune((base_time + timedelta(minutes=15)).timestamp())
    remaining = [entry["state"] for entry in store.to_records()]
    if remaining != ["Charging", 30.0] or store.attribute_exceptions:
        print(f"ERROR: Expected ['Charging', 30.0] and no attribute exceptions after pruning on a record, got {remaining} {store.attribute_exceptions}")
        failed += 1
    else:
        print("✓ Prune on an exact record time keeps nothing older")

    # Test 7d: a non-numeric state in force at the cutoff keeps its value, not the 0.0 placeholder (#5456)
    switch_store = EntityHistory.from_records(
        [
            {"state": "on", "last_updated": base_time.isoformat(), "attributes": {}},
            {"state": "off", "last_updated": (base_time + timedelta(minutes=5)).isoformat(), "attributes": {}},
        ]
    )
    switch_store.prune((base_time + timedelta(minutes=10)).timestamp())
    remaining = [entry["state"] for entry in switch_store.to_records()]
    if remaining != ["off"]:
        print(f"ERROR: Expected ['off'] kept at the cutoff, got {remaining}")
        failed += 1
    else:
        print("✓ A non-numeric state in force at the cutoff is kept")

    # Test 7c: a rarely-changing sensor whose last change (a brief 'unavailable' blip) is older than
    # the cutoff keeps its current value, not just the blip (#5456)
    blip_store = EntityHistory.from_records(
        [
            {"state": "7.0", "last_updated": base_time.isoformat(), "attributes": {}},
            {"state": "unavailable", "last_updated": (base_time + timedelta(days=2)).isoformat(), "attributes": {}},
            {"state": "7.0", "last_updated": (base_time + timedelta(days=2, minutes=1)).isoformat(), "attributes": {}},
        ]
    )
    blip_cutoff = base_time + timedelta(days=3)
    blip_store.prune(blip_cutoff.timestamp())
    remaining = blip_store.to_records()
    if [entry["state"] for entry in remaining] != [7.0] or str2time(remaining[0]["last_updated"]) != blip_cutoff:
        print(f"ERROR: Expected [7.0] stamped at the cutoff, got {remaining}")
        failed += 1
    else:
        print("✓ A sensor with no change since the cutoff keeps its current value")

    # Test 8: records without a valid last_updated are not stored
    bad_store = EntityHistory.from_records([{"state": "1"}, {"state": "2", "last_updated": "junk"}, {"state": "3", "last_updated": base_time.isoformat()}])
    if len(bad_store) != 1:
        print(f"ERROR: Expected 1 valid record stored, got {len(bad_store)}")
        failed += 1
    else:
        print("✓ Records without a valid last_updated are dropped")

    return failed


def _snapshot_fixture():
    """Build a HAHistory holding two entities that exercise every part of the store"""
    base_time = datetime(2025, 12, 25, 10, 0, 0, tzinfo=pytz.UTC)
    ha_history = HAHistory(MockBase())
    ha_history.initialize()

    # Numeric entity whose attributes change part way through (an attribute exception), then pruned so it is anchored
    numeric_id = "sensor.battery_soc"
    numeric = []
    for count in range(12):
        attributes = {"unit": "kWh"} if count != 7 else {"unit": "kWh", "note": "changed"}
        numeric.append({"state": str(count * 0.5), "last_updated": (base_time + timedelta(minutes=5 * count, microseconds=123457)).isoformat(), "attributes": attributes})
    ha_history.add_entity(numeric_id, 7)
    ha_history.update_entity(numeric_id, numeric)
    ha_history.history_data[numeric_id].prune((base_time + timedelta(minutes=12)).timestamp())

    # Entity with non-numeric states mixed in with numeric ones
    text_id = "select.mode"
    text = []
    for count, state in enumerate(["Charging", "unavailable", "3.25", None, "Idle"]):
        text.append({"state": state, "last_updated": (base_time + timedelta(minutes=10 * count)).isoformat(), "attributes": {"options": ["Charging", "Idle"]}})
    ha_history.add_entity(text_id, 30)
    ha_history.update_entity(text_id, text)
    return ha_history


def test_hahistory_snapshot_roundtrip(my_predbat=None):
    """Test export_snapshot() then import_snapshot() restores an identical history store"""
    print("\n=== Testing HAHistory snapshot round trip ===")
    failed = 0

    source = _snapshot_fixture()
    if not source.history_data["sensor.battery_soc"].anchored or not source.history_data["sensor.battery_soc"].attribute_exceptions:
        print("ERROR: Fixture should hold an anchored store with an attribute exception")
        failed += 1
    if not source.history_data["select.mode"].state_exceptions:
        print("ERROR: Fixture should hold state exceptions")
        failed += 1

    data = source.export_snapshot()
    if not isinstance(data, bytes) or not data:
        print("ERROR: export_snapshot() should return non-empty bytes")
        return failed + 1

    target = HAHistory(MockBase())
    target.initialize()
    loaded = target.import_snapshot(data)
    if loaded != 2:
        print(f"ERROR: import_snapshot() should load 2 entities, got {loaded}")
        failed += 1

    if target.history_entities != source.history_entities:
        print(f"ERROR: history_entities differ: {target.history_entities} vs {source.history_entities}")
        failed += 1

    for entity_id, store in source.history_data.items():
        restored = target.history_data.get(entity_id)
        if restored is None:
            print(f"ERROR: {entity_id} missing after import")
            failed += 1
            continue
        if restored.to_records() != store.to_records():
            print(f"ERROR: {entity_id} records differ after import")
            failed += 1
        if restored.anchored != store.anchored or list(restored.epochs) != list(store.epochs):
            print(f"ERROR: {entity_id} anchoring or epochs differ after import")
            failed += 1
        if restored.state_exceptions != store.state_exceptions or restored.attribute_exceptions != store.attribute_exceptions or restored.shared_attributes != store.shared_attributes:
            print(f"ERROR: {entity_id} exceptions or shared attributes differ after import")
            failed += 1

    # The restored store keeps working: a later append lands after the restored data
    last_epoch = target.history_data["select.mode"].epochs[-1]
    target.update_entity("select.mode", [{"state": "Exporting", "last_updated": datetime.fromtimestamp(last_epoch + 60, pytz.UTC).isoformat(), "attributes": {"options": ["Charging", "Idle"]}}])
    if target.history_data["select.mode"].to_records()[-1]["state"] != "Exporting":
        print("ERROR: Restored store should accept new records")
        failed += 1

    if failed == 0:
        print("OK: test_hahistory_snapshot_roundtrip")
    return failed


def test_hahistory_snapshot_rejects_bad_input(my_predbat=None):
    """Test import_snapshot() returns 0 and leaves the store untouched on any bad input"""
    print("\n=== Testing HAHistory snapshot rejects bad input ===")
    failed = 0

    good = _snapshot_fixture().export_snapshot()
    payload = json.loads(zlib.decompress(good))

    def encode(value):
        return zlib.compress(json.dumps(value).encode("utf-8"))

    def variant():
        return json.loads(json.dumps(payload))

    wrong_version = dict(payload, v=2)
    bool_version = dict(payload, v=True)
    short_states = variant()
    short_states["entities"]["select.mode"]["states"] = base64.b64encode(b"\x00" * 8).decode("ascii")
    stray_exception = variant()
    stray_exception["entities"]["select.mode"]["state_exceptions"]["12345.0"] = "x"
    # One good entity followed by a broken one must not half-load the good one
    half_bad = variant()
    half_bad["entities"]["select.mode"]["anchored"] = "yes"
    unsorted = variant()
    epochs = base64.b64decode(unsorted["entities"]["select.mode"]["epochs"])
    unsorted["entities"]["select.mode"]["epochs"] = base64.b64encode(epochs[8:16] + epochs[0:8] + epochs[16:]).decode("ascii")
    bad_base64 = variant()
    bad_base64["entities"]["select.mode"]["epochs"] = "!!not base64!!"
    bad_attributes = variant()
    bad_attributes["entities"]["sensor.battery_soc"]["shared_attributes"] = [1, 2]

    cases = [
        ("empty bytes", b""),
        ("not zlib", b"not zlib"),
        ("not bytes", "a string"),
        ("None", None),
        ("not json", zlib.compress(b"{not json")),
        ("json list", encode([1, 2])),
        ("version 2", encode(wrong_version)),
        ("version True", encode(bool_version)),
        ("states shorter than epochs", encode(short_states)),
        ("exception for an unknown epoch", encode(stray_exception)),
        ("one bad entity", encode(half_bad)),
        ("unsorted epochs", encode(unsorted)),
        ("bad base64", encode(bad_base64)),
        ("shared attributes not a dict", encode(bad_attributes)),
    ]
    for label, data in cases:
        target = HAHistory(MockBase())
        target.initialize()
        try:
            loaded = target.import_snapshot(data)
        except Exception as exc:
            print(f"ERROR: import_snapshot() raised on {label}: {exc}")
            failed += 1
            continue
        if loaded != 0 or target.history_data or target.history_entities:
            print(f"ERROR: {label} should return 0 and leave the store empty, got {loaded}, {list(target.history_data)}")
            failed += 1

    # A bad snapshot leaves an already populated store exactly as it was
    target = _snapshot_fixture()
    before = {entity_id: store.to_records() for entity_id, store in target.history_data.items()}
    entities_before = dict(target.history_entities)
    loaded = target.import_snapshot(encode(half_bad))
    after = {entity_id: store.to_records() for entity_id, store in target.history_data.items()}
    if loaded != 0 or after != before or target.history_entities != entities_before:
        print("ERROR: A bad snapshot should leave a populated store untouched")
        failed += 1

    if failed == 0:
        print("OK: test_hahistory_snapshot_rejects_bad_input")
    return failed


def test_hahistory_snapshot_then_delta(my_predbat=None):
    """Test the 2-minute update after an import only requests data newer than the snapshot"""
    print("\n=== Testing HAHistory snapshot then delta update ===")

    async def run_test():
        failed = 0
        source = _snapshot_fixture()
        data = source.export_snapshot()

        mock_base = MockBase()
        ha_history = HAHistory(mock_base)
        ha_history.initialize()
        mock_ha = MockHAInterface()
        mock_base.components.register_component("ha", mock_ha)
        if ha_history.import_snapshot(data) != 2:
            print("ERROR: Snapshot should load 2 entities")
            return 1

        await ha_history.run(seconds=120, first=False)

        calls = {call["entity_id"]: call for call in mock_ha.get_history_calls}
        for entity_id, store in source.history_data.items():
            call = calls.get(entity_id)
            expected = datetime.fromtimestamp(store.epochs[-1], pytz.UTC)
            if call is None:
                print(f"ERROR: {entity_id} was not refreshed")
                failed += 1
            elif call["from_time"] != expected or call["days"] != 1:
                print(f"ERROR: {entity_id} should request the delta from {expected}, got from_time {call['from_time']} days {call['days']}")
                failed += 1
        return failed

    failed = run_async(run_test())
    if failed == 0:
        print("OK: test_hahistory_snapshot_then_delta")
    return failed


def run_hahistory_tests(my_predbat):
    """Run all HAHistory unit tests"""
    print("\n" + "=" * 80)
    print("HAHistory Unit Tests")
    print("=" * 80)

    failed = 0

    # Basic functionality tests
    failed += test_entity_history_store(my_predbat)
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
    failed += test_hahistory_merge_drops_restamped_anchor(my_predbat)
    failed += test_hahistory_prune_history(my_predbat)
    failed += test_hahistory_prune_empty_history(my_predbat)

    # Async run tests
    failed += test_hahistory_run_first_call(my_predbat)
    failed += test_hahistory_run_no_ha_interface(my_predbat)
    failed += test_hahistory_run_periodic_update(my_predbat)
    failed += test_hahistory_run_hourly_prune(my_predbat)
    failed += test_hahistory_run_no_update_timing(my_predbat)

    # Snapshot tests
    failed += test_hahistory_snapshot_roundtrip(my_predbat)
    failed += test_hahistory_snapshot_rejects_bad_input(my_predbat)
    failed += test_hahistory_snapshot_then_delta(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAHistory tests passed!")
    else:
        print(f"❌ {failed} HAHistory test(s) failed")
    print("=" * 80 + "\n")

    return failed
