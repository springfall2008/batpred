"""
Tests for OctopusAPI async_get_intelligent_devices, covering the flexPlannedDispatches API key
and the energyAddedKwh delta field used by the new Octopus dispatch API.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from octopus import OctopusAPI, DATE_TIME_STR_FORMAT, OCTOPUS_CAR_ENTITY_SPEC


def test_octopus_intelligent_devices_wrapper(my_predbat):
    """Wrapper to run the async dispatch-parsing tests plus the synchronous discovery-catalogue tests."""
    failed = asyncio.run(test_octopus_intelligent_devices(my_predbat))
    failed += test_build_discovery_cars_active_only(my_predbat)
    failed += test_build_discovery_meters_intelligent_go_flag(my_predbat)
    failed += test_build_discovery_cars_entity_map_matches_automatic_config(my_predbat)
    failed += test_build_discovery_meters_direction_distinct(my_predbat)
    failed += test_build_discovery_cars_entities_only_when_published(my_predbat)
    failed += test_discovery_report_not_advanced_while_car_entities_incomplete(my_predbat)
    failed += test_discovery_report_failure_contained_and_retried(my_predbat)
    failed += test_build_discovery_round_trips_through_coordinator_and_redaction(my_predbat)
    return failed


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
    - Test 10: async_update_intelligent_devices prunes a device no longer returned as LIVE
    - Test 11: A transient settings-query failure does not evict a known device
    - Test 12: A successful poll finding no live EVs is distinguishable from a failed poll
    - Test 13: The cache empties when the last EV is deregistered, but survives a failed poll
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

    # ------------------------------------------------------------------
    # Test 10: async_update_intelligent_devices prunes a device Octopus no longer returns as
    # LIVE. Regression for a support ticket where a customer with one EV saw two Octopus
    # Intelligent device IDs in Predbat: a stale/ghost registration (invisible in the Octopus
    # app) was cached on a prior poll and never dropped once it stopped appearing in the live
    # devices() response, permanently occupying a car slot and holding num_cars up.
    # ------------------------------------------------------------------
    print("\n*** Test 10: Stale device pruned when no longer live ***")
    api = make_api()
    api.tariffs = {"import": {"tariffCode": "E-1R-INTELLI-VAR-24-10-29-A", "deviceID": "meter-1"}}

    # Cycle 1: Octopus returns two live devices
    api.async_get_intelligent_devices = AsyncMock(
        return_value={
            "device-real": {"device_id": "device-real", "completed_dispatches": [], "planned_dispatches": []},
            "device-ghost": {"device_id": "device-ghost", "completed_dispatches": [], "planned_dispatches": []},
        }
    )
    await api.async_update_intelligent_devices("test-account")

    if set(api.intelligent_devices.keys()) != {"device-real", "device-ghost"}:
        print(f"ERROR: Expected both devices cached after cycle 1, got {list(api.intelligent_devices.keys())}")
        failed += 1
    else:
        # Cycle 2: Octopus now only returns the real device - device-ghost has dropped out of
        # the live list (re-paired charger, deregistered vehicle, etc.)
        api.async_get_intelligent_devices = AsyncMock(return_value={"device-real": {"device_id": "device-real", "completed_dispatches": [], "planned_dispatches": []}})
        await api.async_update_intelligent_devices("test-account")

        if "device-ghost" in api.intelligent_devices:
            print(f"ERROR: Stale device-ghost was not pruned, still in intelligent_devices: {list(api.intelligent_devices.keys())}")
            failed += 1
        elif "device-real" not in api.intelligent_devices:
            print("ERROR: Real device was incorrectly removed along with the stale one")
            failed += 1
        else:
            print("PASS: Stale device no longer live was pruned, real device retained")

    # ------------------------------------------------------------------
    # Test 11: A device whose per-device settings query transiently fails must not be dropped
    # from the result. Dropping it makes async_update_intelligent_devices treat the device as no
    # longer LIVE and delete it from the cache (Test 10's pruning path), so one flaky GraphQL call
    # evicts a real car - and with the device set now driving automatic_config re-wiring
    # (issue #4648) that would flap the car slots on every blip. Reuse the last known settings
    # instead, so the suspended flag stays put until Octopus actually tells us otherwise.
    # ------------------------------------------------------------------
    print("\n*** Test 11: Transient settings-query failure does not evict a known device ***")
    api = make_api()

    async def mock_query_settings_fail(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return device_data
        elif "get-intelligent-dispatches" in context:
            return {"flexPlannedDispatches": [], "completedDispatches": []}
        elif "get-intelligent-settings" in context:
            return None
        return None

    # The device is already known from an earlier successful poll, and was suspended at the time
    api.intelligent_devices = {"device-abc": {"device_id": "device-abc", "suspended": True, "weekday_target_time": "07:00", "completed_dispatches": [], "planned_dispatches": []}}
    api.async_graphql_query = AsyncMock(side_effect=mock_query_settings_fail)

    result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" not in result:
        print(f"ERROR: Known device evicted by a transient settings-query failure, got {list(result.keys())}")
        failed += 1
    elif result["device-abc"].get("suspended") is not True:
        print(f"ERROR: Expected the last known suspended state to be retained, got {result['device-abc'].get('suspended')}")
        failed += 1
    elif result["device-abc"].get("weekday_target_time") != "07:00":
        print(f"ERROR: Expected the last known charging preferences to be retained, got {result['device-abc'].get('weekday_target_time')}")
        failed += 1
    else:
        print("PASS: Known device retained with its last known settings when the settings query fails")

    # A device we have never seen before has no settings to fall back on, so it is still skipped
    # rather than being wired in with an unknown suspended state.
    api2 = make_api()
    api2.intelligent_devices = {}
    api2.async_graphql_query = AsyncMock(side_effect=mock_query_settings_fail)

    result2 = await api2.async_get_intelligent_devices("test-account", "device-abc")

    if "device-abc" in result2:
        print(f"ERROR: Unknown device should be skipped when its settings cannot be read, got {list(result2.keys())}")
        failed += 1
    else:
        print("PASS: Never-seen device is skipped when its settings query fails")

    # ------------------------------------------------------------------
    # Test 12: "the devices query failed" and "the account has no EVs on it any more" must not
    # look the same to the caller. Both used to return {}, so async_update_intelligent_devices
    # could not tell them apart and skipped pruning for both - which meant deregistering your last
    # EV left it cached and wired to a car slot forever. A failed query returns None; a successful
    # query that simply contains no live EV devices returns {}.
    # ------------------------------------------------------------------
    print("\n*** Test 12: Failed poll and genuinely empty account are distinguishable ***")
    api = make_api()

    async def mock_query_devices_fail(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return None
        return None

    api.async_graphql_query = AsyncMock(side_effect=mock_query_devices_fail)
    failed_result = await api.async_get_intelligent_devices("test-account", "device-abc")

    if failed_result is not None:
        print(f"ERROR: Expected None when the devices query fails, got {failed_result!r}")
        failed += 1
    else:
        print("PASS: Failed devices query reports None")

    # The electricity meter stays in the devices list after the EV is removed, so a genuinely
    # EV-free account still returns a non-empty devices list - just none of type ELECTRIC_VEHICLES.
    meter_only_data = {
        "devices": [
            {
                "deviceType": "ELECTRICITY_METERS",
                "status": {"current": "LIVE"},
                "__typename": "SmartFlexDevice",
                "id": "meter-1",
            }
        ]
    }

    async def mock_query_meter_only(query, context, ignore_errors=False, returns_data=True):
        if "get-intelligent-devices" in context:
            return meter_only_data
        return None

    api2 = make_api()
    api2.async_graphql_query = AsyncMock(side_effect=mock_query_meter_only)
    empty_result = await api2.async_get_intelligent_devices("test-account", "device-abc")

    if empty_result is None:
        print("ERROR: Expected {} for an account with no live EVs, got None")
        failed += 1
    elif empty_result != {}:
        print(f"ERROR: Expected no devices for a meter-only account, got {list(empty_result.keys())}")
        failed += 1
    else:
        print("PASS: Account with no live EVs reports an empty result, not a failure")

    # ------------------------------------------------------------------
    # Test 13: the cache follows that distinction - it empties when the last EV really is gone, and
    # is left alone when the poll simply failed.
    # ------------------------------------------------------------------
    print("\n*** Test 13: Cache empties on a real removal but survives a failed poll ***")
    api = make_api()
    api.tariffs = {"import": {"tariffCode": "E-1R-INTELLI-VAR-24-10-29-A", "deviceID": "meter-1"}}
    api.intelligent_devices = {"device-real": {"device_id": "device-real", "suspended": False, "completed_dispatches": [], "planned_dispatches": []}}

    api.async_get_intelligent_devices = AsyncMock(return_value=None)
    await api.async_update_intelligent_devices("test-account")

    if "device-real" not in api.intelligent_devices:
        print("ERROR: A failed poll wiped the intelligent device cache")
        failed += 1
    else:
        print("PASS: Cache retained when the poll fails")

        api.async_get_intelligent_devices = AsyncMock(return_value={})
        await api.async_update_intelligent_devices("test-account")

        if api.intelligent_devices != {}:
            print(f"ERROR: Expected the cache to empty once the last EV is deregistered, got {list(api.intelligent_devices.keys())}")
            failed += 1
        else:
            print("PASS: Cache empties once the last EV is deregistered")

    if failed == 0:
        print("\n**** All Octopus intelligent devices tests PASSED ****")
    else:
        print(f"\n**** Octopus intelligent devices tests FAILED ({failed} test(s) failed) ****")
    return failed


# ----------------------------------------------------------------------------------------------
# OctopusAPI.build_discovery() - the discovery catalogue reporter (meters, tariffs, cars).
#
# Unlike the async dispatch-parsing tests above, these build a real OctopusAPI against the shared
# my_predbat harness (real get_state_wrapper/set_state_wrapper/get_entity_name), since
# build_discovery()'s entity-existence check (see its docstring) has to be exercised against a
# real state store, not a mock. Each test uses its own account_id so entities published by one
# test cannot be mistaken for another's, since my_predbat's state store is shared across every
# test called from test_octopus_intelligent_devices_wrapper.
# ----------------------------------------------------------------------------------------------


def _make_discovery_api(my_predbat, account_id, automatic=True):
    """A real OctopusAPI instance for build_discovery()/_refresh_discovery_report() tests."""
    return OctopusAPI(my_predbat, key="test-key", account_id=account_id, automatic=automatic)


def _publish_car_entities(my_predbat, api, device_id):
    """Publish all three of one device's car entities in my_predbat's state store, as async_intelligent_update_sensor() would."""
    index_suffix = api.device_id_to_index_suffix(device_id)
    for domain, suffix, _access in OCTOPUS_CAR_ENTITY_SPEC.values():
        my_predbat.set_state_wrapper(api.get_entity_name(domain, suffix, index=index_suffix), "on")


def test_build_discovery_cars_active_only(my_predbat):
    """Two active intelligent devices and one suspended yield exactly two car records - the same filter automatic_config() applies."""
    api = _make_discovery_api(my_predbat, "cars-active-only")
    api.intelligent_devices = {
        "smart-charge-1001": {"suspended": False},
        "smart-charge-1002": {"suspended": False},
        "smart-charge-1003": {"suspended": True},
    }

    report = api.build_discovery()

    device_ids = {record["device_id"] for record in report["cars"]}
    expected = {"octopus:smart-charge-1001", "octopus:smart-charge-1002"}
    if device_ids != expected:
        print(f"ERROR: expected car records for exactly the two active devices {expected}, got {device_ids}")
        return 1
    print("PASS: only active (non-suspended) intelligent devices produce a car record")
    return 0


def test_build_discovery_meters_intelligent_go_flag(my_predbat):
    """An Intelligent GO tariff code yields a meter whose tariff.flags contains intelligent_go."""
    api = _make_discovery_api(my_predbat, "meters-iog-flag")
    api.tariffs = {"import": {"tariffCode": "E-1R-INTELLI-VAR-24-10-29-A", "productCode": "INTELLI-VAR-24-10-29", "deviceID": "meter-1"}}

    report = api.build_discovery()

    flags = report["meters"][0].get("tariff", {}).get("flags", [])
    if "intelligent_go" not in flags:
        print(f"ERROR: expected 'intelligent_go' in tariff.flags, got {flags}")
        return 1
    print("PASS: an Intelligent GO tariff code yields tariff.flags containing intelligent_go")
    return 0


def test_build_discovery_cars_entity_map_matches_automatic_config(my_predbat):
    """The car record's entities point at exactly the entity names automatic_config() wires into apps.yaml for the same device."""
    api = _make_discovery_api(my_predbat, "cars-entity-map")
    device_id = "smart-charge-2001"
    api.intelligent_devices = {device_id: {"suspended": False}}
    _publish_car_entities(my_predbat, api, device_id)
    index_suffix = api.device_id_to_index_suffix(device_id)

    report = api.build_discovery()

    entities = report["cars"][0]["entities"]
    expected = {
        "octopus_intelligent_slot": api.get_entity_name("binary_sensor", "intelligent_dispatch", index=index_suffix),
        "octopus_ready_time": api.get_entity_name("select", "intelligent_target_time", index=index_suffix),
        "octopus_charge_limit": api.get_entity_name("number", "intelligent_target_soc", index=index_suffix),
    }
    failed = 0
    for name, entity_id in expected.items():
        got = entities.get(name, {}).get("entity_id")
        if got != entity_id:
            print(f"ERROR: {name} entity_id mismatch: expected {entity_id}, got {got}")
            failed += 1
    if failed == 0:
        print("PASS: car entities match the same entity names automatic_config() wires into apps.yaml")
    return failed


def test_build_discovery_meters_direction_distinct(my_predbat):
    """An import and an export tariff yield two meter records with distinct direction."""
    api = _make_discovery_api(my_predbat, "meters-two-directions")
    api.tariffs = {
        "import": {"tariffCode": "E-1R-VAR-22-11-01-A", "productCode": "VAR-22-11-01", "deviceID": "meter-import"},
        "export": {"tariffCode": "E-1R-OUTGOING-VAR-22-11-01-A", "productCode": "OUTGOING-VAR-22-11-01", "deviceID": "meter-export"},
    }

    report = api.build_discovery()

    directions = {record["direction"] for record in report["meters"]}
    if len(report["meters"]) != 2 or directions != {"import", "export"}:
        print(f"ERROR: expected two meter records with distinct import/export directions, got {report['meters']}")
        return 1
    print("PASS: import and export tariffs yield two meter records with distinct direction")
    return 0


def test_build_discovery_cars_entities_only_when_published(my_predbat):
    """
    A car's entities dict only lists what actually exists in the state store.

    Octopus publishes octopus_intelligent_slot/octopus_ready_time/octopus_charge_limit
    conditionally, never as a fixed set (see build_discovery()'s docstring) - the catalogue must
    never claim an entity exists that Home Assistant has never seen.
    """
    api = _make_discovery_api(my_predbat, "cars-entities-partial")
    device_id = "smart-charge-3001"
    api.intelligent_devices = {device_id: {"suspended": False}}
    index_suffix = api.device_id_to_index_suffix(device_id)
    # Only the dispatch slot sensor has been published so far - ready_time/charge_limit have not.
    my_predbat.set_state_wrapper(api.get_entity_name("binary_sensor", "intelligent_dispatch", index=index_suffix), "on")

    report = api.build_discovery()

    entities = report["cars"][0].get("entities", {})
    if set(entities) != {"octopus_intelligent_slot"}:
        print(f"ERROR: expected only octopus_intelligent_slot to be reported, got {list(entities)}")
        return 1
    print("PASS: build_discovery() reports only the car entities that actually exist in the state store")
    return 0


def test_discovery_report_not_advanced_while_car_entities_incomplete(my_predbat):
    """
    The reported-marker does not advance while an active device's car entities are incomplete.

    Without this, a report built one cycle too early (see build_discovery()'s ordering docstring)
    would be marked done forever, and the catalogue would permanently describe an incomplete car
    slot even once Octopus goes on to publish the missing entities on a later cycle.
    """
    api = _make_discovery_api(my_predbat, "cars-incomplete-marker")
    device_id = "smart-charge-4001"
    api.intelligent_devices = {device_id: {"suspended": False}}
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    api._refresh_discovery_report()

    failed = 0
    if api.discovery_reported_for is not None:
        print("ERROR: the marker should not advance while the device's car entities are not yet published")
        failed += 1
    if len(reports) != 1:
        print(f"ERROR: expected exactly one report attempt, got {len(reports)}")
        failed += 1

    # Octopus has now published the device's entities - a later cycle's async_intelligent_update_sensor().
    _publish_car_entities(my_predbat, api, device_id)

    api._refresh_discovery_report()

    if api.discovery_reported_for is None:
        print("ERROR: the marker should advance once the active device's car entities are complete")
        failed += 1
    if len(reports) != 2:
        print(f"ERROR: expected a second report attempt once entities were complete, got {len(reports)}")
        failed += 1
    if failed == 0:
        print("PASS: the reported-marker only advances once the active device's car entities are complete")
    return failed


def test_discovery_report_failure_contained_and_retried(my_predbat):
    """
    A bug in build_discovery() must not propagate out of _refresh_discovery_report(), and the
    failed attempt is retried the next time it is called - not lost for the life of the process,
    even though "first" (the one-shot gate on the first of automatic_config()'s three call sites)
    never runs again once run() has returned True once.
    """
    api = _make_discovery_api(my_predbat, "discovery-failure-retry")
    api.tariffs = {"import": {"tariffCode": "E-1R-VAR-22-11-01-A", "productCode": "VAR-22-11-01", "deviceID": "meter-1"}}
    real_build_discovery = api.build_discovery
    api.build_discovery = MagicMock(side_effect=Exception("boom"))
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    api._refresh_discovery_report()

    failed = 0
    if reports:
        print("ERROR: no report should have been recorded on the failing attempt")
        failed += 1
    if api.discovery_reported_for is not None:
        print("ERROR: a failed report must not be marked as reported")
        failed += 1

    # The bug is fixed; calling it again - as the next automatic_config() call site would - retries and succeeds.
    api.build_discovery = real_build_discovery
    api._refresh_discovery_report()

    if len(reports) != 1:
        print(f"ERROR: the retried report should now succeed, got {len(reports)} reports")
        failed += 1
    if api.discovery_reported_for is None:
        print("ERROR: the marker should advance once the retried report succeeds")
        failed += 1
    if failed == 0:
        print("PASS: a build_discovery() failure is contained and retried on the next call, not lost forever")
    return failed


def test_build_discovery_round_trips_through_coordinator_and_redaction(my_predbat):
    """
    Feed build_discovery()'s output through the real Coordinator.report()/assemble() and then
    through the real Redactor.

    This is the first discovery reporter carrying genuinely sensitive data (MPANs, account ids),
    so the check that matters most here is not just "nothing was silently dropped by validation"
    (the round-trip check that caught a real bug in each of the previous two reporters) but that
    the redacted catalogue never publishes a raw MPAN or account identifier in the clear, while the
    tariff and product codes - public Octopus product data, not customer data - DO survive.
    """
    from coordinator import Coordinator
    from mock_base import MockBase as SharedMockBase

    mpan = "1200023305967"
    account_id = "A-1234ABCD"
    api = _make_discovery_api(my_predbat, account_id)
    api.mpan = mpan
    api.tariffs = {
        "import": {
            "tariffCode": "E-1R-INTELLI-VAR-24-10-29-A",
            "productCode": "INTELLI-VAR-24-10-29",
            "deviceID": "meter-import",
            "standing": [{"value_inc_vat": 45.32, "valid_from": None, "valid_to": None}],
        },
        "export": {"tariffCode": "E-1R-AGILE-OUTGOING-19-05-13-A", "productCode": "AGILE-OUTGOING-19-05-13", "deviceID": "meter-export"},
    }
    device_id = "smart-charge-5001"
    api.intelligent_devices = {device_id: {"suspended": False, "vehicle_battery_size_in_kwh": 75.0, "charge_point_power_in_kw": 7.4}}
    _publish_car_entities(my_predbat, api, device_id)

    report = api.build_discovery()

    coordinator = Coordinator(SharedMockBase())
    coordinator.report("octopus", report)
    cleaned = coordinator.reports["octopus"]

    failed = 0

    def check(condition, message):
        """Record one failed assertion, printing its message, without aborting the remaining checks."""
        nonlocal failed
        if not condition:
            print("ERROR: " + message)
            failed += 1

    # Nothing intended for a typed container was silently dropped by validation.
    import_record = next(r for r in cleaned["meters"] if r["direction"] == "import")
    check(import_record.get("account_ids", {}).get("mpan") == mpan, "mpan dropped or altered by validation: {}".format(import_record.get("account_ids")))
    check(import_record.get("account_ids", {}).get("account") == account_id, "account dropped or altered by validation: {}".format(import_record.get("account_ids")))
    check(import_record.get("tariff", {}).get("info", {}).get("tariff_code") == "E-1R-INTELLI-VAR-24-10-29-A", "tariff_code dropped by validation: {}".format(import_record.get("tariff")))
    check(import_record.get("tariff", {}).get("info", {}).get("product_code") == "INTELLI-VAR-24-10-29", "product_code dropped by validation: {}".format(import_record.get("tariff")))
    check("intelligent_go" in import_record.get("tariff", {}).get("flags", []), "intelligent_go flag dropped by validation: {}".format(import_record.get("tariff")))
    check(import_record.get("ratings", {}).get("standing_charge_p") == 45.32, "standing_charge_p dropped or altered by validation: {}".format(import_record.get("ratings")))
    export_record = next(r for r in cleaned["meters"] if r["direction"] == "export")
    check("agile" in export_record.get("tariff", {}).get("flags", []), "agile flag dropped by validation: {}".format(export_record.get("tariff")))
    car_record = cleaned["cars"][0]
    check(len(car_record.get("entities", {})) == len(OCTOPUS_CAR_ENTITY_SPEC), "not every car entity survived validation: {}".format(car_record.get("entities")))
    check(car_record.get("ratings", {}).get("vehicle_battery_kwh") == 75.0, "vehicle battery size dropped or altered by validation: {}".format(car_record.get("ratings")))
    check(car_record.get("ratings", {}).get("charge_point_power_kw") == 7.4, "charge point power dropped or altered by validation: {}".format(car_record.get("ratings")))

    coordinator.assemble()
    catalogue_text = str(coordinator.catalogue())

    check(mpan not in catalogue_text, "the raw MPAN appears in the clear in the redacted catalogue")
    check(account_id not in catalogue_text, "the raw account id appears in the clear in the redacted catalogue")
    check("E-1R-INTELLI-VAR-24-10-29-A" in catalogue_text, "the tariff code should survive in the clear, but is missing from the redacted catalogue")
    check("INTELLI-VAR-24-10-29" in catalogue_text, "the product code should survive in the clear, but is missing from the redacted catalogue")

    if failed == 0:
        print("PASS: build_discovery() round-trips through the real Coordinator and Redactor - MPAN/account pseudonymised, tariff/product codes kept in the clear")
    return failed
