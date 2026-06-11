"""
Tests for Octopus cache save/load functions (storage-component backed)
"""

import asyncio
import tempfile
import shutil
from octopus import OctopusAPI
from storage import StorageComponent, StorageLocalFiles


def test_octopus_cache_wrapper(my_predbat):
    return asyncio.run(test_octopus_cache(my_predbat))


class _MockComponents:
    """Minimal components mock that returns a pre-configured storage component."""

    def __init__(self, storage):
        """Initialise with a storage instance."""
        self._storage = storage

    def get_component(self, name):
        """Return the mocked storage for 'storage', None for others."""
        if name == "storage":
            return self._storage
        return None


def _attach_storage(api, temp_dir):
    """Attach a real StorageComponent backed by a temp directory to the api instance."""
    storage = StorageComponent(api.base)
    storage.backend = StorageLocalFiles(temp_dir, api.base.log)
    api.base.components = _MockComponents(storage)
    return storage


async def test_octopus_cache(my_predbat):
    """
    Test save_octopus_cache and load_octopus_cache functions via the storage component.

    Tests:
    - Test 1: Save and load user cache data (account_data, saving_sessions, intelligent_devices, kraken_token)
    - Test 2: Load with no prior save initialises empty (no crash)
    - Test 3: None values are normalised to empty dicts on load
    - Test 4: get_tariff_cache_key sanitisation (pure string logic)
    """
    print("**** Running Octopus cache save/load tests ****")
    failed = False

    # Create a temporary directory for cache testing (shared backend across instances)
    test_cache_dir = tempfile.mkdtemp(prefix="predbat_test_cache_")

    try:
        # Test 1: Save and load user cache data round-trips via storage
        print("\n*** Test 1: Save and load user cache data ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
        _attach_storage(api, test_cache_dir)

        api.account_data = {"account": {"number": "A-12345678", "electricityAgreements": [{"meterPoint": {"mpan": "1234567890123"}}]}}
        api.saving_sessions = {"events": [{"id": "event1", "startAt": "2024-06-15T18:00:00+00:00"}], "account": {"hasJoinedCampaign": True}}
        api.intelligent_devices = {"device123": {"planned_dispatches": [{"start": "2024-06-15T23:00:00+00:00", "end": "2024-06-16T05:00:00+00:00"}]}}
        api.graphql_token = "test-token-12345"
        api.tariffs = {}

        await api.save_octopus_cache()

        # Verify the user cache round-trips via the storage component
        stored = await api.storage.load("octopus_user", "account")
        if not stored:
            print("ERROR: User cache was not stored")
            failed = True
        else:
            # Create new API instance sharing the same storage backend and load
            api2 = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
            _attach_storage(api2, test_cache_dir)

            await api2.load_octopus_cache()

            if api2.account_data != api.account_data:
                print("ERROR: account_data mismatch. Expected: {}, Got: {}".format(api.account_data, api2.account_data))
                failed = True
            elif api2.saving_sessions != api.saving_sessions:
                print("ERROR: saving_sessions mismatch. Expected: {}, Got: {}".format(api.saving_sessions, api2.saving_sessions))
                failed = True
            elif api2.intelligent_devices != api.intelligent_devices:
                print("ERROR: intelligent_devices mismatch. Expected: {}, Got: {}".format(api.intelligent_devices, api2.intelligent_devices))
                failed = True
            elif api2.graphql_token != api.graphql_token:
                print("ERROR: graphql_token mismatch. Expected: {}, Got: {}".format(api.graphql_token, api2.graphql_token))
                failed = True
            else:
                print("PASS: User cache data saved and loaded correctly")

        # Test 2: Load with no prior save initialises empty (no crash)
        print("\n*** Test 2: Load with no prior save initialises empty ***")
        empty_cache_dir = tempfile.mkdtemp(prefix="predbat_test_cache_empty_")
        try:
            api3 = OctopusAPI(my_predbat, key="test-key", account_id="nonexistent", automatic=False)
            _attach_storage(api3, empty_cache_dir)

            await api3.load_octopus_cache()

            if api3.account_data != {}:
                print("ERROR: Expected empty account_data, got: {}".format(api3.account_data))
                failed = True
            elif api3.saving_sessions != {}:
                print("ERROR: Expected empty saving_sessions, got: {}".format(api3.saving_sessions))
                failed = True
            elif api3.intelligent_devices != {}:
                print("ERROR: Expected empty intelligent_devices, got: {}".format(api3.intelligent_devices))
                failed = True
            elif api3.tariffs != {}:
                print("ERROR: Expected empty tariffs, got: {}".format(api3.tariffs))
                failed = True
            else:
                print("PASS: Missing cache initialised to empty dicts")
        finally:
            shutil.rmtree(empty_cache_dir, ignore_errors=True)

        # Test 3: None values are normalised to empty dicts on load
        print("\n*** Test 3: Verify None values are handled correctly ***")
        none_cache_dir = tempfile.mkdtemp(prefix="predbat_test_cache_none_")
        try:
            api6 = OctopusAPI(my_predbat, key="test-key", account_id="test-none", automatic=False)
            _attach_storage(api6, none_cache_dir)

            api6.account_data = None
            api6.saving_sessions = None
            api6.intelligent_devices = None
            api6.graphql_token = None
            api6.tariffs = {}

            await api6.save_octopus_cache()

            api7 = OctopusAPI(my_predbat, key="test-key", account_id="test-none", automatic=False)
            _attach_storage(api7, none_cache_dir)

            await api7.load_octopus_cache()

            if api7.account_data != {}:
                print("ERROR: Expected empty dict for None account_data, got: {}".format(api7.account_data))
                failed = True
            elif api7.saving_sessions != {}:
                print("ERROR: Expected empty dict for None saving_sessions, got: {}".format(api7.saving_sessions))
                failed = True
            elif api7.intelligent_devices != {}:
                print("ERROR: Expected empty dict for None intelligent_devices, got: {}".format(api7.intelligent_devices))
                failed = True
            else:
                print("PASS: None values handled correctly")
        finally:
            shutil.rmtree(none_cache_dir, ignore_errors=True)

        # Test 4: get_tariff_cache_key sanitisation (pure string logic)
        print("\n*** Test 4: get_tariff_cache_key sanitisation ***")
        api8 = OctopusAPI(my_predbat, key="test-key", account_id="test-key-san", automatic=False)
        key = api8.get_tariff_cache_key({"productCode": "AGILE/22", "tariffCode": "E-1R\\AGILE-22-A"})
        if "/" in key or "\\" in key:
            print("ERROR: get_tariff_cache_key did not sanitise path separators: {}".format(key))
            failed = True
        elif key != "AGILE_22_E-1R_AGILE-22-A":
            print("ERROR: Unexpected sanitised key: {}".format(key))
            failed = True
        else:
            print("PASS: get_tariff_cache_key sanitised correctly")

    finally:
        # Clean up temp directory
        shutil.rmtree(test_cache_dir, ignore_errors=True)

    if not failed:
        print("\n**** All Octopus cache tests PASSED ****")

    return failed
