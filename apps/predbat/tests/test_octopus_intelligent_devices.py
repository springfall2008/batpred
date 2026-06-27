"""
Tests for OctopusAPI async_get_intelligent_devices, covering the flexPlannedDispatches API key
and the energyAddedKwh delta field used by the new Octopus dispatch API.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from octopus import OctopusAPI, DATE_TIME_STR_FORMAT


def test_octopus_intelligent_devices_wrapper(my_predbat):
    """Wrapper to run async tests."""
    return asyncio.run(test_octopus_intelligent_devices(my_predbat))


async def test_octopus_intelligent_devices(my_predbat):
    """
    Tests for async_get_intelligent_devices.

    Tests:
    - Test 1: flexPlannedDispatches key is read (not old plannedDispatches key)
    - Test 2: Old plannedDispatches key returns no planned dispatches (regression guard)
    - Test 3: energyAddedKwh field used for delta (new API field)
    - Test 4: delta field used as fallback when energyAddedKwh absent (backwards compat)
    - Test 5: Future planned dispatch is kept in planned list
    - Test 6: Completed dispatches are parsed correctly
    - Test 7: Planned dispatch with missing start/end is skipped
    - Test 8: In-progress flex dispatch not promoted to completed but trimmed to remainder (issue #4114)
    - Test 9: Future flex dispatch is left untrimmed in planned
    """
    print("**** Running Octopus intelligent devices tests ****")
    failed = 0

    # Use a fixed reference time for all tests so timestamps are deterministic
    # regardless of what previous tests may have set on my_predbat.
    ref_now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    future_start = (ref_now + timedelta(hours=1)).strftime(DATE_TIME_STR_FORMAT)
    future_end = (ref_now + timedelta(hours=2)).strftime(DATE_TIME_STR_FORMAT)
    past_start = (ref_now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT)
    past_end = (ref_now - timedelta(days=1) + timedelta(hours=1)).strftime(DATE_TIME_STR_FORMAT)

    device_data = {
        "devices": [
            {
                "deviceType": "ELECTRIC_VEHICLES",
                "status": {"current": "LIVE"},
                "__typename": "SmartFlexVehicle",
                "make": "Tesla",
                "model": "Model 3",
                "id": "device-abc",
            }
        ],
        "chargePointVariants": [],
        "electricVehicles": [{"make": "Tesla", "models": [{"model": "Model 3", "batterySize": 75.0}]}],
    }

    settings_data = {
        "devices": [
            {
                "id": "device-abc",
                "status": {"isSuspended": False},
                "chargingPreferences": {
                    "weekdayTargetTime": "07:00",
                    "weekdayTargetSoc": 80,
                    "weekendTargetTime": "09:00",
                    "weekendTargetSoc": 90,
                    "minimumSoc": 20,
                    "maximumSoc": 100,
                },
            }
        ]
    }

    def make_api():
        """Create a fresh OctopusAPI instance with now_utc_exact fixed to ref_now."""

        class FixedTimeOctopusAPI(OctopusAPI):
            """OctopusAPI subclass that pins now_utc_exact to the test reference time."""

            @property
            def now_utc_exact(self):
                """Return the fixed test reference time."""
                return ref_now

        api = FixedTimeOctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
        api.get_intelligent_completed_dispatches = MagicMock(return_value=[])
        api.get_state_wrapper = MagicMock(return_value=[])
        return api

    # ------------------------------------------------------------------
    # Test 1: flexPlannedDispatches key populates planned_dispatches
    # ------------------------------------------------------------------
    print("\n*** Test 1: flexPlannedDispatches key is read ***")
    api = make_api()

    dispatch_data_flex = {
        "flexPlannedDispatches": [
            {
                "start": future_start,
                "end": future_end,
                "energyAddedKwh": "10.5",
                "type": "smart-charge",
                "meta": {"source": "smart-charge", "location": "AT_HOME"},
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_flex(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_flex
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_flex)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" not in result:
        print("ERROR: device-abc not found in result")
        failed += 1
    else:
        planned = result["device-abc"].get("planned_dispatches", [])
        if len(planned) != 1:
            print(f"ERROR: Expected 1 planned dispatch, got {len(planned)}")
            failed += 1
        elif planned[0].get("charge_in_kwh") != 10.5:
            print(f"ERROR: Expected charge_in_kwh=10.5, got {planned[0].get('charge_in_kwh')}")
            failed += 1
        else:
            print("PASS: flexPlannedDispatches key correctly populates planned dispatches")

    # ------------------------------------------------------------------
    # Test 2: Old plannedDispatches key is NOT read (regression guard)
    # ------------------------------------------------------------------
    print("\n*** Test 2: Old plannedDispatches key returns no planned dispatches ***")
    api = make_api()

    dispatch_data_old_key = {
        "plannedDispatches": [  # old key — must NOT be read
            {
                "start": future_start,
                "end": future_end,
                "delta": "8.0",
                "type": "smart-charge",
                "meta": {},
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_old_key(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_old_key
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_old_key)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" not in result:
        print("ERROR: device-abc not found in result")
        failed += 1
    else:
        planned = result["device-abc"].get("planned_dispatches", [])
        if len(planned) != 0:
            print(f"ERROR: Old 'plannedDispatches' key should NOT be read — expected 0 planned, got {len(planned)}")
            failed += 1
        else:
            print("PASS: Old plannedDispatches key correctly ignored")

    # ------------------------------------------------------------------
    # Test 3: energyAddedKwh field used for delta
    # ------------------------------------------------------------------
    print("\n*** Test 3: energyAddedKwh field used for delta ***")
    api = make_api()

    dispatch_data_energy = {
        "flexPlannedDispatches": [
            {
                "start": future_start,
                "end": future_end,
                "energyAddedKwh": "15.25",
                "delta": "0.0",  # should be ignored when energyAddedKwh present
                "type": "smart-charge",
                "meta": {},
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_energy(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_energy
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_energy)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" in result:
        planned = result["device-abc"].get("planned_dispatches", [])
        if len(planned) == 1 and planned[0].get("charge_in_kwh") == 15.25:
            print("PASS: energyAddedKwh takes precedence over delta")
        else:
            print(f"ERROR: Expected charge_in_kwh=15.25, got {planned[0].get('charge_in_kwh') if planned else 'no dispatches'}")
            failed += 1
    else:
        print("ERROR: device-abc not found in result")
        failed += 1

    # ------------------------------------------------------------------
    # Test 4: delta field used as fallback when energyAddedKwh absent
    # ------------------------------------------------------------------
    print("\n*** Test 4: delta field used as fallback ***")
    api = make_api()

    dispatch_data_delta = {
        "flexPlannedDispatches": [
            {
                "start": future_start,
                "end": future_end,
                "delta": "7.5",
                "type": "smart-charge",
                "meta": {},
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_delta(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_delta
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_delta)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" in result:
        planned = result["device-abc"].get("planned_dispatches", [])
        if len(planned) == 1 and planned[0].get("charge_in_kwh") == 7.5:
            print("PASS: delta field used as fallback when energyAddedKwh absent")
        else:
            print(f"ERROR: Expected charge_in_kwh=7.5, got {planned[0].get('charge_in_kwh') if planned else 'no dispatches'}")
            failed += 1
    else:
        print("ERROR: device-abc not found in result")
        failed += 1

    # ------------------------------------------------------------------
    # Test 5: Future planned dispatch is kept in planned list
    # ------------------------------------------------------------------
    print("\n*** Test 5: Future planned dispatch stays in planned ***")
    api = make_api()

    dispatch_data_future = {
        "flexPlannedDispatches": [
            {
                "start": future_start,
                "end": future_end,
                "energyAddedKwh": "5.0",
                "type": "smart-charge",
                "meta": {"source": "smart-charge", "location": "AT_HOME"},
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_future(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_future
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_future)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" in result:
        planned = result["device-abc"].get("planned_dispatches", [])
        completed = result["device-abc"].get("completed_dispatches", [])
        if len(planned) == 1 and len(completed) == 0:
            print("PASS: Future planned dispatch stays in planned list")
        else:
            print(f"ERROR: Expected 1 planned / 0 completed, got {len(planned)} planned / {len(completed)} completed")
            failed += 1
    else:
        print("ERROR: device-abc not found in result")
        failed += 1

    # ------------------------------------------------------------------
    # Test 6: Completed dispatches are parsed correctly
    # ------------------------------------------------------------------
    print("\n*** Test 6: Completed dispatches parsed correctly ***")
    api = make_api()

    dispatch_data_completed = {
        "flexPlannedDispatches": [],
        "completedDispatches": [
            {
                "start": past_start,
                "end": past_end,
                "delta": "12.0",
                "meta": {"source": "smart-charge", "location": "AT_HOME"},
            }
        ],
    }

    async def mock_query_completed(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_completed
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_completed)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" in result:
        completed = result["device-abc"].get("completed_dispatches", [])
        if len(completed) == 1 and completed[0].get("charge_in_kwh") == 12.0:
            print("PASS: Completed dispatch parsed correctly")
        else:
            print(f"ERROR: Expected 1 completed dispatch with charge_in_kwh=12.0, got {completed}")
            failed += 1
    else:
        print("ERROR: device-abc not found in result")
        failed += 1

    # ------------------------------------------------------------------
    # Test 7: Planned dispatch with missing start/end is skipped
    # ------------------------------------------------------------------
    print("\n*** Test 7: Planned dispatch with missing start/end is skipped ***")
    api = make_api()

    dispatch_data_missing = {
        "flexPlannedDispatches": [
            {"energyAddedKwh": "5.0", "type": "smart-charge", "meta": {}},  # no start/end
            {
                "start": future_start,
                "end": future_end,
                "energyAddedKwh": "3.0",
                "type": "smart-charge",
                "meta": {},
            },
        ],
        "completedDispatches": [],
    }

    async def mock_query_missing(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_missing
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_missing)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" in result:
        planned = result["device-abc"].get("planned_dispatches", [])
        if len(planned) == 1:
            print("PASS: Dispatch missing start/end correctly skipped, valid dispatch kept")
        else:
            print(f"ERROR: Expected 1 valid planned dispatch, got {len(planned)}")
            failed += 1
    else:
        print("ERROR: device-abc not found in result")
        failed += 1

    # ------------------------------------------------------------------
    # Test 8: In-progress flex planned dispatch is NOT promoted to completed (issue #4114),
    # but IS trimmed to the remaining portion so already-delivered energy is not double counted.
    # A flexPlannedDispatches entry that started a few minutes ago must stay in the planned
    # list (not be fabricated into completed_dispatches - Octopus routinely withdraws such
    # provisional SMART flex slots), with its start advanced to now and charge_in_kwh scaled
    # down to the remaining time.
    # ------------------------------------------------------------------
    print("\n*** Test 8: In-progress flex dispatch not promoted, trimmed to remaining portion ***")
    api = make_api()

    # Slot started 10 min ago and ends 20 min from now -> 30 min total, 20 min remaining (2/3)
    in_progress_start = (ref_now - timedelta(minutes=10)).strftime(DATE_TIME_STR_FORMAT)
    in_progress_end = (ref_now + timedelta(minutes=20)).strftime(DATE_TIME_STR_FORMAT)
    expected_trimmed_start = ref_now.strftime(DATE_TIME_STR_FORMAT)
    expected_trimmed_kwh = round(0.367 * 20 / 30, 4)  # scaled to remaining portion, dp4
    dispatch_data_in_progress = {
        "flexPlannedDispatches": [
            {
                "start": in_progress_start,
                "end": in_progress_end,
                "energyAddedKwh": "0.367",
                "type": "smart-charge",
                "meta": {"source": "SMART"},  # no location, as flexPlannedDispatches carries no location
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_in_progress(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_in_progress
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_in_progress)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" not in result:
        print("ERROR: device-abc not found in result")
        failed += 1
    else:
        planned = result["device-abc"].get("planned_dispatches", [])
        completed = result["device-abc"].get("completed_dispatches", [])
        if len(completed) != 0:
            print(f"ERROR: In-progress flex dispatch was promoted to completed (got {len(completed)} completed): {completed}")
            failed += 1
        elif len(planned) != 1:
            print(f"ERROR: Expected 1 planned dispatch (kept in planned), got {len(planned)}")
            failed += 1
        elif planned[0].get("start") != expected_trimmed_start:
            print(f"ERROR: Expected in-progress slot start trimmed to now ({expected_trimmed_start}), got {planned[0].get('start')}")
            failed += 1
        elif planned[0].get("charge_in_kwh") != expected_trimmed_kwh:
            print(f"ERROR: Expected charge_in_kwh scaled to remaining ({expected_trimmed_kwh}), got {planned[0].get('charge_in_kwh')}")
            failed += 1
        else:
            print("PASS: In-progress flex dispatch kept in planned, not promoted, and trimmed to remaining portion")

    # ------------------------------------------------------------------
    # Test 9: Future flex dispatch (not yet started) is left untrimmed in planned
    # ------------------------------------------------------------------
    print("\n*** Test 9: Future flex dispatch is not trimmed ***")
    api = make_api()

    future_only_start = (ref_now + timedelta(minutes=30)).strftime(DATE_TIME_STR_FORMAT)
    future_only_end = (ref_now + timedelta(minutes=60)).strftime(DATE_TIME_STR_FORMAT)
    dispatch_data_future_only = {
        "flexPlannedDispatches": [
            {
                "start": future_only_start,
                "end": future_only_end,
                "energyAddedKwh": "2.0",
                "type": "smart-charge",
                "meta": {"source": "SMART"},
            }
        ],
        "completedDispatches": [],
    }

    async def mock_query_future_only(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return dispatch_data_future_only
        elif "get-intelligent-settings" in context:
            return settings_data
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_future_only)
    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" not in result:
        print("ERROR: device-abc not found in result")
        failed += 1
    else:
        planned = result["device-abc"].get("planned_dispatches", [])
        if len(planned) != 1:
            print(f"ERROR: Expected 1 planned dispatch, got {len(planned)}")
            failed += 1
        elif planned[0].get("start") != future_only_start:
            print(f"ERROR: Future slot start should be untouched ({future_only_start}), got {planned[0].get('start')}")
            failed += 1
        elif planned[0].get("charge_in_kwh") != 2.0:
            print(f"ERROR: Future slot charge_in_kwh should be untouched (2.0), got {planned[0].get('charge_in_kwh')}")
            failed += 1
        else:
            print("PASS: Future flex dispatch left untrimmed in planned")

    if failed == 0:
        print("\n**** All Octopus intelligent devices tests PASSED ****")
    else:
        print(f"\n**** Octopus intelligent devices tests FAILED ({failed} test(s) failed) ****")
    return failed
