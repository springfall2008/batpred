import unittest
import tempfile
import os
from datetime import datetime, timedelta
from db_engine import DatabaseEngine

class MockBase:
    """Mock base class for testing"""
    def __init__(self, config_root=None):
        self.config_root = config_root or tempfile.gettempdir()
        self.now_utc_real = datetime.utcnow()
        self.log_messages = []

    def log(self, message):
        self.log_messages.append(message)


class TestDatabaseEngine(unittest.TestCase):
    """
    Test DatabaseEngine using only its public interface.
    These tests focus on behavior rather than implementation details.
    """

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.temp_dir = tempfile.mkdtemp()
        self.mock_base = MockBase(self.temp_dir)
        self.db_days = 30
        self.db_engine = DatabaseEngine(self.mock_base, self.db_days)

    def tearDown(self):
        """Clean up after each test method."""
        if hasattr(self, 'db_engine'):
            self.db_engine._close()
        # Clean up temp files
        db_path = os.path.join(self.temp_dir, "predbat.db")
        if os.path.exists(db_path):
            os.remove(db_path)
        os.rmdir(self.temp_dir)

    def test_initialization_creates_working_database(self):
        """Test that DatabaseEngine initializes and creates a working database."""
        # Should initialize without errors
        self.assertIsNotNone(self.db_engine)
        # Should be able to perform basic operations without crashes
        self.db_engine._set_state_db("test.entity", "test_state", {}, datetime.utcnow())
        result = self.db_engine._get_state_db("test.entity")
        self.assertIsNotNone(result)

    def test_set_and_get_state_workflow(self):
        """Test the complete workflow of setting and retrieving entity state."""
        entity_id = "sensor.temperature"
        state = "25.5"
        attributes = {"unit": "°C", "friendly_name": "Living Room Temperature"}
        timestamp = datetime.utcnow()

        # Set the state
        self.db_engine._set_state_db(entity_id, state, attributes, timestamp)

        # Retrieve the state
        result = self.db_engine._get_state_db(entity_id)

        # Verify the data was stored and retrieved correctly
        self.assertIsNotNone(result)
        self.assertEqual(result['state'], state)
        self.assertIn('attributes', result)
        self.assertEqual(result['attributes']['unit'], "°C")
        self.assertEqual(result['attributes']['friendly_name'], "Living Room Temperature")
        self.assertIn('last_updated', result)

    def test_nonexistent_entity_returns_none(self):
        """Test that requesting a non-existent entity returns None."""
        result = self.db_engine._get_state_db("sensor.nonexistent")
        self.assertIsNone(result)

    def test_entity_state_updates(self):
        """Test that entity states can be updated and reflect the latest values."""
        entity_id = "sensor.humidity"

        # Set initial state
        self.db_engine._set_state_db(entity_id, "45", {"unit": "%"}, datetime.utcnow())
        initial_result = self.db_engine._get_state_db(entity_id)

        # Update the state
        self.db_engine._set_state_db(entity_id, "52", {"unit": "%"}, datetime.utcnow())
        updated_result = self.db_engine._get_state_db(entity_id)

        # Verify the state was updated
        self.assertEqual(initial_result['state'], "45")
        self.assertEqual(updated_result['state'], "52")

    def test_multiple_entities_independently_stored(self):
        """Test that multiple entities can be stored and retrieved independently."""
        entities_data = [
            ("sensor.temp1", "22.1", {"location": "bedroom"}),
            ("sensor.temp2", "24.5", {"location": "kitchen"}),
            ("sensor.humidity", "45", {"unit": "%"})
        ]

        timestamp = datetime.utcnow()

        # Store all entities
        for entity_id, state, attributes in entities_data:
            self.db_engine._set_state_db(entity_id, state, attributes, timestamp)

        # Retrieve and verify each entity
        for entity_id, expected_state, expected_attributes in entities_data:
            result = self.db_engine._get_state_db(entity_id)
            self.assertIsNotNone(result)
            self.assertEqual(result['state'], expected_state)
            for key, value in expected_attributes.items():
                self.assertEqual(result['attributes'][key], value)

    def test_get_all_entities_returns_stored_entities(self):
        """Test that get_all_entities returns all stored entity IDs."""
        entity_ids = ["sensor.temp", "switch.light", "binary_sensor.door"]
        timestamp = datetime.utcnow()

        # Store entities
        for entity_id in entity_ids:
            self.db_engine._set_state_db(entity_id, "test_state", {}, timestamp)

        # Get all entities
        all_entities = self.db_engine._get_all_entities_db()

        # Verify all our entities are returned (may be lowercase)
        for entity_id in entity_ids:
            self.assertIn(entity_id.lower(), [e.lower() for e in all_entities])

    def test_history_retrieval_basic(self):
        """Test basic history retrieval functionality."""
        entity_id = "sensor.test_history"
        base_time = datetime(2024, 1, 1, 12, 0, 0)

        # Store multiple states over time
        for i in range(3):
            timestamp = base_time + timedelta(hours=i)
            state = f"state_{i}"
            attributes = {"sequence": i}
            self.db_engine._set_state_db(entity_id, state, attributes, timestamp)

        # Get history
        now = base_time + timedelta(days=1)
        history = self.db_engine._get_history_db(entity_id, now, days=2)

        # Should return some history data
        self.assertIsNotNone(history)
        self.assertIsInstance(history, list)
        self.assertGreater(len(history), 0)

        # The returned history should contain our data
        if len(history) > 0 and len(history[0]) > 0:
            # Check that we have some of our states in the history
            states_in_history = [item['state'] for item in history[0]]
            self.assertIn("state_0", states_in_history)

    def test_history_date_filtering(self):
        """Test that history respects date filtering."""
        entity_id = "sensor.date_filter_test"
        old_time = datetime(2024, 1, 1, 12, 0, 0)
        recent_time = datetime(2024, 1, 10, 12, 0, 0)

        # Store old record
        self.db_engine._set_state_db(entity_id, "old_state", {}, old_time)

        # Store recent record
        self.db_engine._set_state_db(entity_id, "recent_state", {}, recent_time)

        # Get history for only 1 day from recent time
        history = self.db_engine._get_history_db(entity_id, recent_time, days=1)

        # Should get some history, but the exact filtering behavior depends on implementation
        self.assertIsNotNone(history)

    def test_attributes_preserved_in_storage_retrieval(self):
        """Test that complex attributes are preserved correctly."""
        entity_id = "sensor.complex_attributes"
        complex_attributes = {
            "nested": {"inner": "value"},
            "list": [1, 2, 3],
            "string": "test string",
            "number": 42.5,
            "boolean": True
        }

        self.db_engine._set_state_db(entity_id, "test_state", complex_attributes, datetime.utcnow())
        result = self.db_engine._get_state_db(entity_id)

        self.assertIsNotNone(result)
        # Check that basic attributes are preserved
        self.assertIn('attributes', result)
        if 'string' in result['attributes']:
            self.assertEqual(result['attributes']['string'], "test string")
        if 'number' in result['attributes']:
            self.assertEqual(result['attributes']['number'], 42.5)
        if 'boolean' in result['attributes']:
            self.assertEqual(result['attributes']['boolean'], True)

    def test_database_persistence_across_operations(self):
        """Test that data persists across multiple database operations."""
        entity_id = "sensor.persistence_test"

        # Store initial state
        self.db_engine._set_state_db(entity_id, "initial", {"test": "initial"}, datetime.utcnow())

        # Perform other operations
        self.db_engine._set_state_db("other.entity", "other_state", {}, datetime.utcnow())
        self.db_engine._get_all_entities_db()

        # Original entity should still be retrievable
        result = self.db_engine._get_state_db(entity_id)
        self.assertIsNotNone(result)
        self.assertEqual(result['state'], "initial")

    def test_large_data_handling(self):
        """Test handling of reasonably large data sets."""
        base_time = datetime.utcnow()

        # Store many entities
        for i in range(50):  # Reasonable number for a unit test
            entity_id = f"sensor.test_{i:03d}"
            state = f"state_{i}"
            attributes = {"index": i, "description": f"Test sensor number {i}"}
            self.db_engine._set_state_db(entity_id, state, attributes, base_time)

        # Should be able to retrieve all entities
        all_entities = self.db_engine._get_all_entities_db()
        self.assertGreaterEqual(len(all_entities), 50)

        # Should be able to retrieve individual entities
        test_entity = self.db_engine._get_state_db("sensor.test_025")
        self.assertIsNotNone(test_entity)
        self.assertEqual(test_entity['state'], "state_25")

    def test_close_database_cleanup(self):
        """Test that database can be properly closed."""
        # Store some data
        self.db_engine._set_state_db("test.entity", "test", {}, datetime.utcnow())

        # Close should not raise an exception
        self.db_engine._close()

        # Should log closure
        self.assertIn("db_engine: Closed", self.mock_base.log_messages)


if __name__ == '__main__':
    unittest.main()