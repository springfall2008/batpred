# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for HAInterface state management operations.

Tests cover get_state, update_state, update_states, set_state, and db_mirror_list tracking.
"""

from unittest.mock import patch
from tests.test_hainterface_common import MockBase, MockDatabaseManager, create_mock_requests_response, create_ha_interface


def test_hainterface_get_state_no_entity(my_predbat=None):
    """Test get_state() with no entity_id returns full state dict"""
    print("\n=== Testing HAInterface get_state() no entity_id ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False)

    # Initialize with some state data
    ha_interface.state_data = {"sensor.battery": {"state": "50", "attributes": {"unit": "kWh"}}, "sensor.solar": {"state": "100", "attributes": {}}}

    # Get all state
    result = ha_interface.get_state()

    if result != ha_interface.state_data:
        print("ERROR: Should return full state_data dict")
        failed += 1
    else:
        print("✓ Returned full state_data dict")

    return failed


def test_hainterface_get_state_cached(my_predbat=None):
    """Test get_state() returns cached state"""
    print("\n=== Testing HAInterface get_state() cached ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.state_data = {"sensor.battery": {"state": "50", "attributes": {"unit": "kWh", "friendly_name": "Battery"}}}

    # Test basic state retrieval
    result = ha_interface.get_state("sensor.battery")
    if result != "50":
        print(f"ERROR: Expected '50', got '{result}'")
        failed += 1
    else:
        print("✓ Retrieved cached state value")

    # Test with attribute
    result = ha_interface.get_state("sensor.battery", attribute="unit")
    if result != "kWh":
        print(f"ERROR: Expected 'kWh', got '{result}'")
        failed += 1
    else:
        print("✓ Retrieved cached attribute")

    # Test with default for missing attribute
    result = ha_interface.get_state("sensor.battery", attribute="missing", default="default_value")
    if result != "default_value":
        print(f"ERROR: Expected 'default_value', got '{result}'")
        failed += 1
    else:
        print("✓ Returned default for missing attribute")

    # Test raw mode
    result = ha_interface.get_state("sensor.battery", raw=True)
    if not isinstance(result, dict) or result.get("state") != "50":
        print("ERROR: raw=True should return full state dict")
        failed += 1
    else:
        print("✓ Returned raw state dict")

    return failed


def test_hainterface_get_state_missing_entity(my_predbat=None):
    """Test get_state() with missing entity returns default"""
    print("\n=== Testing HAInterface get_state() missing entity ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.state_data = {}

    result = ha_interface.get_state("sensor.missing", default="default_value")
    if result != "default_value":
        print(f"ERROR: Expected 'default_value', got '{result}'")
        failed += 1
    else:
        print("✓ Returned default for missing entity")

    return failed


def test_hainterface_get_state_case_insensitive(my_predbat=None):
    """Test get_state() is case-insensitive"""
    print("\n=== Testing HAInterface get_state() case sensitivity ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.state_data = {"sensor.battery": {"state": "50", "attributes": {}}}

    # Try mixed case
    result = ha_interface.get_state("Sensor.Battery")
    if result != "50":
        print("ERROR: Should be case-insensitive")
        failed += 1
    else:
        print("✓ Case-insensitive lookup works")

    return failed


def test_hainterface_get_state_db_mirror_tracking(my_predbat=None):
    """Test get_state() adds entity to db_mirror_list"""
    print("\n=== Testing HAInterface get_state() db_mirror_list tracking ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.state_data = {"sensor.battery": {"state": "50", "attributes": {}}}
    ha_interface.db_mirror_list = {}

    # Access entity
    ha_interface.get_state("sensor.battery")

    if "sensor.battery" not in ha_interface.db_mirror_list:
        print("ERROR: Entity should be added to db_mirror_list")
        failed += 1
    else:
        print("✓ Entity tracked in db_mirror_list")

    return failed


def test_hainterface_update_state_item_basic(my_predbat=None):
    """Test update_state_item() stores state in cache"""
    print("\n=== Testing HAInterface update_state_item() basic ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.db_enable = False
    ha_interface.db_mirror_ha = False
    ha_interface.state_data = {}

    item = {"state": "42", "attributes": {"unit": "kWh"}, "last_changed": "2025-12-25T10:00:00Z"}

    ha_interface.update_state_item(item, "sensor.battery", nodb=True)

    if "sensor.battery" not in ha_interface.state_data:
        print("ERROR: State should be cached")
        failed += 1
    elif ha_interface.state_data["sensor.battery"]["state"] != "42":
        print("ERROR: State value incorrect")
        failed += 1
    else:
        print("✓ State cached correctly")

    return failed


def test_hainterface_update_state_item_db_mirror(my_predbat=None):
    """Test update_state_item() calls DatabaseManager when db_mirror_ha enabled"""
    print("\n=== Testing HAInterface update_state_item() DB mirroring ===")
    failed = 0

    mock_base = MockBase()
    mock_db = MockDatabaseManager()
    mock_base.components.register_component("db", mock_db)

    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=True, db_mirror_ha=True, db_primary=False)
    ha_interface.db_enable = True
    ha_interface.db_mirror_ha = True
    ha_interface.db_primary = False
    ha_interface.db_manager = mock_db
    ha_interface.db_mirror_list = {"sensor.battery": True}
    ha_interface.state_data = {}

    item = {"state": "42", "attributes": {"unit": "kWh"}, "last_changed": "2025-12-25T10:00:00.000000+00:00"}

    ha_interface.update_state_item(item, "sensor.battery", nodb=False)

    # Verify DB was called
    if len(mock_db.set_state_calls) == 0:
        print("ERROR: DatabaseManager should be called")
        failed += 1
    else:
        print("✓ DatabaseManager set_state_db called")

    # Verify state still cached
    if "sensor.battery" not in ha_interface.state_data:
        print("ERROR: State should also be cached")
        failed += 1
    else:
        print("✓ State cached alongside DB mirror")

    return failed


def test_hainterface_update_state_with_api(my_predbat=None):
    """Test update_state() fetches from API"""
    print("\n=== Testing HAInterface update_state() with API ===")
    failed = 0

    mock_base = MockBase()
    # Create interface with API mode (ha_key set, no DB)
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.state_data = {}

    mock_response_data = {"state": "50", "attributes": {"unit": "kWh"}, "entity_id": "sensor.battery"}

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, mock_response_data)

        ha_interface.update_state("sensor.battery")

        # Verify API was called
        if not mock_get.called:
            print("ERROR: API should be called")
            failed += 1
        else:
            print("✓ API called")

        # Verify state cached
        if "sensor.battery" not in ha_interface.state_data:
            print("ERROR: State should be cached")
            failed += 1
        elif ha_interface.state_data["sensor.battery"]["state"] != "50":
            print("ERROR: State value incorrect")
            failed += 1
        else:
            print("✓ State cached from API")

    return failed


def test_hainterface_update_state_db_primary(my_predbat=None):
    """Test update_state() routes to DB when db_primary mode"""
    print("\n=== Testing HAInterface update_state() DB primary ===")
    failed = 0

    mock_base = MockBase()
    mock_db = MockDatabaseManager()
    mock_db.state_data["sensor.battery"] = {"state": "75", "attributes": {}, "last_changed": "2025-12-25T10:00:00Z"}

    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=True, db_mirror_ha=False, db_primary=True)
    ha_interface.db_enable = True
    ha_interface.db_primary = True
    ha_interface.ha_key = None
    ha_interface.db_manager = mock_db
    ha_interface.state_data = {}

    ha_interface.update_state("sensor.battery")

    # Verify DB was queried
    if "sensor.battery" not in mock_db.get_state_calls:
        print("ERROR: DatabaseManager should be called")
        failed += 1
    else:
        print("✓ DatabaseManager queried")

    # Verify state cached
    if "sensor.battery" not in ha_interface.state_data:
        print("ERROR: State should be cached")
        failed += 1
    elif ha_interface.state_data["sensor.battery"]["state"] != "75":
        print("ERROR: State value incorrect")
        failed += 1
    else:
        print("✓ State cached from DB")

    return failed


def test_hainterface_update_states_bulk(my_predbat=None):
    """Test update_states() bulk fetches from API"""
    print("\n=== Testing HAInterface update_states() bulk ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.ha_key = "test_key"
    ha_interface.db_enable = False
    ha_interface.state_data = {}

    mock_response_data = [
        {"entity_id": "sensor.battery", "state": "50", "attributes": {"unit": "kWh"}},
        {"entity_id": "sensor.solar", "state": "100", "attributes": {"unit": "W"}},
    ]

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, mock_response_data)

        ha_interface.update_states()

        # Verify API called
        if not mock_get.called:
            print("ERROR: API should be called")
            failed += 1
        else:
            print("✓ API called")

        # Verify both entities cached
        if len(ha_interface.state_data) != 2:
            print(f"ERROR: Expected 2 entities, got {len(ha_interface.state_data)}")
            failed += 1
        elif "sensor.battery" not in ha_interface.state_data or "sensor.solar" not in ha_interface.state_data:
            print("ERROR: Missing entities in cache")
            failed += 1
        else:
            print("✓ All entities cached")

    return failed


def test_hainterface_update_states_db_primary(my_predbat=None):
    """Test update_states() routes to DB in db_primary mode"""
    print("\n=== Testing HAInterface update_states() DB primary ===")
    failed = 0

    mock_base = MockBase()
    mock_db = MockDatabaseManager()
    mock_db.state_data = {
        "sensor.battery": {"state": "50", "attributes": {}, "last_changed": "2025-12-25T10:00:00Z"},
        "sensor.solar": {"state": "100", "attributes": {}, "last_changed": "2025-12-25T10:00:00Z"},
    }

    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=True, db_mirror_ha=False, db_primary=True)
    ha_interface.db_enable = True
    ha_interface.db_primary = True
    ha_interface.ha_key = None
    ha_interface.db_manager = mock_db
    ha_interface.state_data = {}

    ha_interface.update_states()

    # Verify DB queried
    if len(mock_db.get_state_calls) == 0:
        print("ERROR: DatabaseManager should be called")
        failed += 1
    else:
        print("✓ DatabaseManager queried")

    # Verify entities cached
    if len(ha_interface.state_data) != 2:
        print(f"ERROR: Expected 2 entities, got {len(ha_interface.state_data)}")
        failed += 1
    else:
        print("✓ All entities cached from DB")

    return failed


def test_hainterface_set_state_basic(my_predbat=None):
    """Test set_state() with API only"""
    print("\n=== Testing HAInterface set_state() basic ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.ha_key = "test_key"
    ha_interface.db_enable = False
    ha_interface.db_mirror_ha = False
    ha_interface.state_data = {}

    with patch("ha.requests.post") as mock_post, patch("ha.requests.get") as mock_get:
        mock_post.return_value = create_mock_requests_response(200, {})
        mock_get.return_value = create_mock_requests_response(200, {"state": "75", "attributes": {"unit": "kWh"}, "entity_id": "sensor.battery"})

        ha_interface.set_state("sensor.battery", "75", {"unit": "kWh"})

        # Verify POST called
        if not mock_post.called:
            print("ERROR: POST API should be called")
            failed += 1
        else:
            print("✓ POST API called")

        # Verify GET called (for update_state)
        if not mock_get.called:
            print("ERROR: GET API should be called for refresh")
            failed += 1
        else:
            print("✓ GET API called to refresh state")

    return failed


def test_hainterface_set_state_db_mirror(my_predbat=None):
    """Test set_state() with DB mirroring enabled"""
    print("\n=== Testing HAInterface set_state() DB mirroring ===")
    failed = 0

    mock_base = MockBase()
    mock_db = MockDatabaseManager()

    # DB mirroring requires ha_key (API mode) with db_mirror_ha=True
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=True, db_mirror_ha=True, db_primary=False)
    ha_interface.db_manager = mock_db
    ha_interface.state_data = {}

    # Add entity to db_mirror_list so it will be mirrored
    ha_interface.db_mirror_list["sensor.battery"] = True

    with patch("ha.requests.post") as mock_post, patch("ha.requests.get") as mock_get:
        mock_post.return_value = create_mock_requests_response(200, {})
        mock_get.return_value = create_mock_requests_response(200, {"state": "80", "attributes": {"unit": "kWh"}})

        ha_interface.set_state("sensor.battery", "80", {"unit": "kWh"})

    # Verify DB called
    if len(mock_db.set_state_calls) == 0:
        print("ERROR: DatabaseManager should be called")
        failed += 1
    else:
        db_call = mock_db.set_state_calls[0]
        if db_call["entity_id"] != "sensor.battery" or db_call["state"] != "80":
            print("ERROR: Wrong parameters to set_state_db")
            failed += 1
        else:
            print("✓ DatabaseManager set_state_db called correctly")

    # Verify state cached
    if "sensor.battery" not in ha_interface.state_data:
        print("ERROR: State should be cached")
        failed += 1
    else:
        print("✓ State cached")

    return failed


def test_hainterface_set_state_db_primary(my_predbat=None):
    """Test set_state() in DB primary mode (no API)"""
    print("\n=== Testing HAInterface set_state() DB primary ===")
    failed = 0

    mock_base = MockBase()
    mock_db = MockDatabaseManager()

    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=True, db_mirror_ha=False, db_primary=True)
    ha_interface.ha_key = None
    ha_interface.db_enable = True
    ha_interface.db_mirror_ha = False
    ha_interface.db_primary = True
    ha_interface.db_manager = mock_db
    ha_interface.state_data = {}

    with patch("ha.requests.post") as mock_post:
        ha_interface.set_state("sensor.battery", "90", {})

        # Verify NO API call
        if mock_post.called:
            print("ERROR: API should not be called in DB primary mode")
            failed += 1
        else:
            print("✓ No API call in DB primary mode")

    # Verify DB called
    if len(mock_db.set_state_calls) == 0:
        print("ERROR: DatabaseManager should be called")
        failed += 1
    else:
        print("✓ DatabaseManager called")

    return failed


def test_hainterface_db_mirror_list_tracking(my_predbat=None):
    """Test db_mirror_list is tracked across operations"""
    print("\n=== Testing HAInterface db_mirror_list tracking ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=True, db_mirror_ha=True, db_primary=False)
    ha_interface.ha_key = "test_key"
    ha_interface.db_enable = True
    ha_interface.db_mirror_ha = True
    ha_interface.state_data = {"sensor.battery": {"state": "50", "attributes": {}}}
    ha_interface.db_mirror_list = {}

    # Test get_state adds to list
    ha_interface.get_state("sensor.battery")
    if "sensor.battery" not in ha_interface.db_mirror_list:
        print("ERROR: get_state should add to db_mirror_list")
        failed += 1
    else:
        print("✓ get_state adds to db_mirror_list")

    # Test update_state adds to list
    ha_interface.db_mirror_list = {}
    with patch("ha.requests.get"):
        ha_interface.update_state("sensor.solar")
    if "sensor.solar" not in ha_interface.db_mirror_list:
        print("ERROR: update_state should add to db_mirror_list")
        failed += 1
    else:
        print("✓ update_state adds to db_mirror_list")

    # Test set_state adds to list
    ha_interface.db_mirror_list = {}
    mock_db = MockDatabaseManager()
    ha_interface.db_manager = mock_db
    with patch("ha.requests.post") as mock_post, patch("ha.requests.get") as mock_get:
        mock_post.return_value = create_mock_requests_response(200, {})
        mock_get.return_value = create_mock_requests_response(200, {"state": "100", "attributes": {}})
        ha_interface.set_state("sensor.grid", "100", {})
    if "sensor.grid" not in ha_interface.db_mirror_list:
        print("ERROR: set_state should add to db_mirror_list")
        failed += 1
    else:
        print("✓ set_state adds to db_mirror_list")

    return failed


def run_hainterface_state_tests(my_predbat):
    """Run all HAInterface state management tests"""
    print("\n" + "=" * 80)
    print("HAInterface State Management Tests")
    print("=" * 80)

    failed = 0

    # get_state tests
    failed += test_hainterface_get_state_no_entity(my_predbat)
    failed += test_hainterface_get_state_cached(my_predbat)
    failed += test_hainterface_get_state_missing_entity(my_predbat)
    failed += test_hainterface_get_state_case_insensitive(my_predbat)
    failed += test_hainterface_get_state_db_mirror_tracking(my_predbat)

    # update_state_item tests
    failed += test_hainterface_update_state_item_basic(my_predbat)
    failed += test_hainterface_update_state_item_db_mirror(my_predbat)

    # update_state tests
    failed += test_hainterface_update_state_with_api(my_predbat)
    failed += test_hainterface_update_state_db_primary(my_predbat)

    # update_states tests
    failed += test_hainterface_update_states_bulk(my_predbat)
    failed += test_hainterface_update_states_db_primary(my_predbat)

    # set_state tests
    failed += test_hainterface_set_state_basic(my_predbat)
    failed += test_hainterface_set_state_db_mirror(my_predbat)
    failed += test_hainterface_set_state_db_primary(my_predbat)

    # db_mirror_list tracking
    failed += test_hainterface_db_mirror_list_tracking(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAInterface state tests passed!")
    else:
        print(f"❌ {failed} HAInterface state test(s) failed")
    print("=" * 80 + "\n")

    return failed
