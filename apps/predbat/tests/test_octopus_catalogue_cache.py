"""
Tests that the global Octopus EV/charge-point catalogue is cached rather than
re-downloaded on every intelligent-device poll.

electricVehicles and chargePointVariants are a static, account-independent
reference catalogue (~136KB, ~188 manufacturers). Bundling them into the
per-account device query re-downloaded them every couple of minutes per
customer, which is heavy enough to attract edge rate limiting.

The catalogue is cached under the "octopus" storage module, which the SaaS
KeyDB backend routes to a shared namespace - so one fetch serves every instance.
"""

import asyncio
import os
import shutil
from octopus import OctopusAPI
from storage import StorageComponent, StorageLocalFiles


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


CATALOGUE = {
    "electricVehicles": [{"make": "Tesla", "models": [{"model": "Model 3", "batterySize": "57.5"}]}],
    "chargePointVariants": [{"make": "Wallbox", "models": [{"model": "Pulsar", "powerInKw": "7.4"}]}],
}

DEVICES = {
    "devices": [
        {
            "id": "device-1",
            "provider": "TEST",
            "deviceType": "ELECTRIC_VEHICLES",
            "status": {"current": "LIVE"},
            "__typename": "SmartFlexVehicle",
            "make": "Tesla",
            "model": "Model 3",
        }
    ]
}

SETTINGS = {"devices": [{"id": "device-1", "status": {"isSuspended": False}, "chargingPreferences": {"weekdayTargetTime": "07:00", "weekdayTargetSoc": 80}}]}


def _build_api(my_predbat, cache_dir, calls):
    """Build an OctopusAPI backed by real storage in cache_dir, recording query contexts.

    The caller must restore my_predbat.components afterwards - the caller saves it before use -
    because the test runner shares one my_predbat across the whole registry.
    """
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    storage = StorageComponent(api.base)
    storage.backend = StorageLocalFiles(cache_dir, api.base.log)
    api.base.components = _MockComponents(storage)

    async def fake_query(query, request_context, **kwargs):
        """Record the query context and return canned data for it."""
        calls.append(request_context)
        if request_context == "get-vehicle-catalogue":
            return dict(CATALOGUE)
        if request_context == "get-intelligent-devices":
            return dict(DEVICES)
        if request_context == "get-intelligent-settings":
            return dict(SETTINGS)
        if request_context == "get-intelligent-dispatches":
            return {"flexPlannedDispatches": [], "completedDispatches": []}
        return None

    api.async_graphql_query = fake_query
    return api


def test_octopus_catalogue_cache_wrapper(my_predbat):
    return asyncio.run(test_octopus_catalogue_cache(my_predbat))


async def test_octopus_catalogue_cache(my_predbat):
    """
    Tests:
    - Test 1: the static catalogue is fetched once and reused across polls
    - Test 2: a second instance sharing the storage backend reuses the cache
    - Test 3: battery size is still resolved correctly from the cached catalogue
    """
    print("**** Running Octopus catalogue cache tests ****")
    failed = False

    cache_dir = "./test_catalogue_cache"
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)

    # The runner shares a single my_predbat across the whole registry, so the injected
    # components mock must be undone or every later test sees it
    original_components = my_predbat.components

    try:
        # Test 1: repeated polls on one instance must not refetch the catalogue
        print("\n*** Test 1: catalogue fetched once across repeated polls ***")
        calls = []
        api = _build_api(my_predbat, cache_dir, calls)

        await api.async_get_intelligent_devices("test-account", "device-1")
        result = await api.async_get_intelligent_devices("test-account", "device-1")

        catalogue_calls = calls.count("get-vehicle-catalogue")
        device_calls = calls.count("get-intelligent-devices")

        if catalogue_calls != 1:
            print("ERROR: Expected the catalogue to be fetched once, got {} fetches".format(catalogue_calls))
            failed = True
        elif device_calls != 2:
            print("ERROR: Expected 2 per-account device queries, got {}".format(device_calls))
            failed = True
        else:
            print("PASS: catalogue fetched once, per-account query still ran each poll")

        # Test 2: a second instance sharing the backend reuses the cached catalogue
        print("\n*** Test 2: second instance reuses the shared cache ***")
        calls2 = []
        api2 = _build_api(my_predbat, cache_dir, calls2)
        await api2.async_get_intelligent_devices("test-account", "device-1")

        if calls2.count("get-vehicle-catalogue") != 0:
            print("ERROR: Second instance re-fetched the catalogue instead of using the shared cache")
            failed = True
        else:
            print("PASS: second instance served the catalogue from the shared cache")

        # Test 3: the cached catalogue still resolves the vehicle battery size
        print("\n*** Test 3: battery size resolved from cached catalogue ***")
        device = (result or {}).get("device-1", {})
        battery = device.get("vehicle_battery_size_in_kwh")
        if battery != "57.5":
            print("ERROR: Expected battery size 57.5 from the cached catalogue, got {}".format(battery))
            failed = True
        else:
            print("PASS: battery size resolved correctly from the cached catalogue")
    finally:
        my_predbat.components = original_components
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)

    if failed:
        print("**** Octopus catalogue cache tests FAILED ****")
    else:
        print("**** All Octopus catalogue cache tests PASSED ****")
    return failed
