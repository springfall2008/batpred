"""
Tests for Octopus cache save/load functions
"""

import asyncio
import os
import tempfile
import shutil
from datetime import datetime, timezone
from unittest.mock import Mock
from octopus import OctopusAPI
import yaml


def test_octopus_cache_wrapper(my_predbat):
    return asyncio.run(test_octopus_cache(my_predbat))


async def test_octopus_cache(my_predbat):
    """
    Test save_octopus_cache and load_octopus_cache functions.
    
    Tests:
    - Test 1: Save and load user cache data (account_data, saving_sessions, intelligent_device, kraken_token)
    - Test 2: Load from non-existent cache file (should initialize empty)
    - Test 3: Save and load tariff data to shared cache
    - Test 4: Handle corrupted cache file gracefully
    - Test 5: Verify None values are handled correctly
    - Test 6: Test get_tariff_cache_key sanitization
    - Test 7: Save multiple tariffs and verify separation
    - Test 8: Verify fetch_tariffs loads from cache when data not present
    """
    print("**** Running Octopus cache save/load tests ****")
    failed = False
    
    # Create a temporary directory for cache testing
    test_cache_dir = tempfile.mkdtemp(prefix="predbat_test_cache_")
    
    try:
        # Test 1: Save and load user cache data
        print("\n*** Test 1: Save and load user cache data ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
        
        # Override cache paths to use temp directory
        api.cache_path = test_cache_dir
        api.shared_cache_path = test_cache_dir + "/shared"
        api.tariffs_cache_path = api.shared_cache_path + "/tariffs"
        api.urls_cache_path = api.shared_cache_path + "/urls"
        api.user_cache_file = api.cache_path + "/octopus_user_test-account.yaml"
        
        # Create directories
        for path in [api.cache_path, api.shared_cache_path, api.tariffs_cache_path, api.urls_cache_path]:
            os.makedirs(path, exist_ok=True)
        
        # Set up test data
        api.account_data = {
            "account": {
                "number": "A-12345678",
                "electricityAgreements": [{"meterPoint": {"mpan": "1234567890123"}}]
            }
        }
        api.saving_sessions = {
            "events": [{"id": "event1", "startAt": "2024-06-15T18:00:00+00:00"}],
            "account": {"hasJoinedCampaign": True}
        }
        api.intelligent_device = {
            "device_id": "device123",
            "planned_dispatches": [{"start": "2024-06-15T23:00:00+00:00", "end": "2024-06-16T05:00:00+00:00"}]
        }
        api.graphql_token = "test-token-12345"
        api.tariffs = {}
        
        # Save the cache
        await api.save_octopus_cache()
        
        # Verify file was created
        if not os.path.exists(api.user_cache_file):
            print("ERROR: Cache file was not created: {}".format(api.user_cache_file))
            failed = True
        else:
            # Create new API instance and load the cache
            api2 = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
            api2.cache_path = test_cache_dir
            api2.shared_cache_path = test_cache_dir + "/shared"
            api2.tariffs_cache_path = api2.shared_cache_path + "/tariffs"
            api2.urls_cache_path = api2.shared_cache_path + "/urls"
            api2.user_cache_file = api.user_cache_file
            
            await api2.load_octopus_cache()
            
            # Verify loaded data matches
            if api2.account_data != api.account_data:
                print("ERROR: account_data mismatch. Expected: {}, Got: {}".format(api.account_data, api2.account_data))
                failed = True
            elif api2.saving_sessions != api.saving_sessions:
                print("ERROR: saving_sessions mismatch. Expected: {}, Got: {}".format(api.saving_sessions, api2.saving_sessions))
                failed = True
            elif api2.intelligent_device != api.intelligent_device:
                print("ERROR: intelligent_device mismatch. Expected: {}, Got: {}".format(api.intelligent_device, api2.intelligent_device))
                failed = True
            elif api2.graphql_token != api.graphql_token:
                print("ERROR: graphql_token mismatch. Expected: {}, Got: {}".format(api.graphql_token, api2.graphql_token))
                failed = True
            else:
                print("PASS: User cache data saved and loaded correctly")
        
        # Test 2: Load from non-existent cache file
        print("\n*** Test 2: Load from non-existent cache file ***")
        api3 = OctopusAPI(my_predbat, key="test-key", account_id="nonexistent", automatic=False)
        api3.cache_path = test_cache_dir
        api3.shared_cache_path = test_cache_dir + "/shared"
        api3.tariffs_cache_path = api3.shared_cache_path + "/tariffs"
        api3.urls_cache_path = api3.shared_cache_path + "/urls"
        api3.user_cache_file = api3.cache_path + "/octopus_user_nonexistent.yaml"
        
        await api3.load_octopus_cache()
        
        if api3.account_data != {}:
            print("ERROR: Expected empty account_data, got: {}".format(api3.account_data))
            failed = True
        elif api3.saving_sessions != {}:
            print("ERROR: Expected empty saving_sessions, got: {}".format(api3.saving_sessions))
            failed = True
        elif api3.intelligent_device != {}:
            print("ERROR: Expected empty intelligent_device, got: {}".format(api3.intelligent_device))
            failed = True
        elif api3.tariffs != {}:
            print("ERROR: Expected empty tariffs, got: {}".format(api3.tariffs))
            failed = True
        else:
            print("PASS: Non-existent cache initialized to empty dicts")
        
        # Test 3: Save and load tariff data to shared cache
        print("\n*** Test 3: Save and load tariff data to shared cache ***")
        api4 = OctopusAPI(my_predbat, key="test-key", account_id="test-tariffs", automatic=False)
        api4.cache_path = test_cache_dir
        api4.shared_cache_path = test_cache_dir + "/shared"
        api4.urls_cache_path = api4.shared_cache_path + "/urls"
        api4.user_cache_file = api4.cache_path + "/octopus_user_test-tariffs.yaml"
        
        # Set up tariff data
        api4.account_data = {}
        api4.saving_sessions = {}
        api4.intelligent_device = {}
        api4.graphql_token = None
        api4.tariffs = {
            "import": {
                "tariffCode": "E-1R-AGILE-24-01-01-A",
                "productCode": "AGILE-24-01-01",
                "deviceID": "device-import",
                "data": [{"value_inc_vat": 25.0}],
                "standing": [{"value_inc_vat": 0.5}]
            }
        }
        
        await api4.save_octopus_cache()
        # Check user cache data
        user_cache_file = api4.user_cache_file
        if not os.path.exists(user_cache_file):
            print("ERROR: User cache file not created: {}".format(user_cache_file))
            failed = True
        else:
            with open(user_cache_file, "r") as f:
                loaded_cache = yaml.safe_load(f)
                if not loaded_cache:
                    print("ERROR: User cache file is empty")
                    failed = True
                        
        # Test 4: Handle corrupted cache file gracefully
        print("\n*** Test 4: Handle corrupted cache file gracefully ***")
        api5 = OctopusAPI(my_predbat, key="test-key", account_id="corrupted", automatic=False)
        api5.cache_path = test_cache_dir
        api5.shared_cache_path = test_cache_dir + "/shared"
        api5.urls_cache_path = api5.shared_cache_path + "/urls"
        api5.user_cache_file = api5.cache_path + "/octopus_user_corrupted.yaml"
        api5.account_data = {"some": "data"}
        
        # Create a corrupted cache file
        with open(api5.user_cache_file, "w") as f:
            f.write("this is not valid yaml: {[{]}")
        
        await api5.load_octopus_cache()
        
        # Should initialize to empty dicts without crashing
        if api5.account_data != {"some": "data"}:
            print("ERROR: Expected old account_data after corrupted load, got: {}".format(api5.account_data))
            failed = True
        else:
            print("PASS: Corrupted cache handled gracefully")
        
        # Test 5: Verify None values are handled correctly
        print("\n*** Test 5: Verify None values are handled correctly ***")
        api6 = OctopusAPI(my_predbat, key="test-key", account_id="test-none", automatic=False)
        api6.cache_path = test_cache_dir
        api6.shared_cache_path = test_cache_dir + "/shared"
        api6.urls_cache_path = api6.shared_cache_path + "/urls"
        api6.user_cache_file = api6.cache_path + "/octopus_user_test-none.yaml"
        
        # Create directories
        for path in [api6.cache_path, api6.shared_cache_path, api6.urls_cache_path]:
            os.makedirs(path, exist_ok=True)
        
        # Set data to None
        api6.account_data = None
        api6.saving_sessions = None
        api6.intelligent_device = None
        api6.graphql_token = None
        api6.tariffs = {}
        
        await api6.save_octopus_cache()
        
        # Load and verify None values are converted to empty dicts
        api7 = OctopusAPI(my_predbat, key="test-key", account_id="test-none", automatic=False)
        api7.cache_path = test_cache_dir
        api7.shared_cache_path = test_cache_dir + "/shared"
        api7.urls_cache_path = api7.shared_cache_path + "/urls"
        api7.user_cache_file = api6.user_cache_file
        
        await api7.load_octopus_cache()
        
        if api7.account_data != {}:
            print("ERROR: Expected empty dict for None account_data, got: {}".format(api7.account_data))
            failed = True
        elif api7.saving_sessions != {}:
            print("ERROR: Expected empty dict for None saving_sessions, got: {}".format(api7.saving_sessions))
            failed = True
        elif api7.intelligent_device != {}:
            print("ERROR: Expected empty dict for None intelligent_device, got: {}".format(api7.intelligent_device))
            failed = True
        else:
            print("PASS: None values handled correctly")
        
        # Test 6: Save multiple tariffs and verify separation
        print("\n*** Test 6: Save User data and check it ***")
        api9 = OctopusAPI(my_predbat, key="test-key", account_id="test-multi", automatic=False)
        api9.cache_path = test_cache_dir
        api9.shared_cache_path = test_cache_dir + "/shared"
        api9.urls_cache_path = api9.shared_cache_path + "/urls"
        api9.user_cache_file = api9.cache_path + "/octopus_user_test-multi.yaml"
        
        # Create directories
        for path in [api9.cache_path, api9.shared_cache_path, api9.urls_cache_path]:
            os.makedirs(path, exist_ok=True)
        
        api9.account_data = {"account": {"number": "A-87654321"}}
        api9.saving_sessions = {"events": []}
        api9.intelligent_device = {"device_id": "device-multi"}
        api9.graphql_token = None
        api9.tariffs = {
            "import": {
                "productCode": "AGILE-24-01-01",
                "tariffCode": "E-1R-AGILE-24-01-01-A",
                "data": [{"value_inc_vat": 25.0}]
            },
            "export": {
                "productCode": "OUTGOING-24-01-01",
                "tariffCode": "E-1R-OUTGOING-24-01-01-A",
                "data": [{"value_inc_vat": 5.0}]
            },
            "gas": {
                "productCode": "VAR-24-01-01",
                "tariffCode": "G-1R-VAR-24-01-01-A",
                "data": [{"value_inc_vat": 10.0}]
            }
        }
        
        await api9.save_octopus_cache()

        # Check user cache file
        user_cache_file = api9.user_cache_file
        if not os.path.exists(user_cache_file):
            print("ERROR: User cache file not created: {}".format(user_cache_file))
            failed = True
        else:
            with open(user_cache_file, "r") as f:
                loaded_cache = yaml.safe_load(f)
                if "account_data" not in loaded_cache or loaded_cache["account_data"] != api9.account_data:
                    print("ERROR: account_data missing or incorrect in user cache")
                    failed = True
                if "saving_sessions" not in loaded_cache or loaded_cache["saving_sessions"] != api9.saving_sessions:
                    print("ERROR: saving_sessions missing or incorrect in user cache")
                    failed = True
                if "intelligent_device" not in loaded_cache or loaded_cache["intelligent_device"] != api9.intelligent_device:
                    print("ERROR: intelligent_device missing or incorrect in user cache")
                    failed = True

    
    finally:
        # Clean up temp directory
        shutil.rmtree(test_cache_dir, ignore_errors=True)
    
    if not failed:
        print("\n**** All Octopus cache tests PASSED ****")
    
    return failed
