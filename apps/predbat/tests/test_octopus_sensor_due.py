"""
Tests for the OctopusAPI intelligent sensor update scheduling (sensor_due)

The intelligent dispatch fetch and sensor are meant to be refreshed every
2 minutes. The component scheduler (ComponentBase.start) is not
minute-aligned: it sleeps in 5 second chunks and does not account for the
time run() itself takes, so run() invocations drift relative to the wall
clock. A refresh gate keyed off wall-clock even minutes
(count_minutes % 2 == 0) can therefore land only on odd minutes and starve
the sensor indefinitely. The gate must instead be based on the age of the
last sensor update.
"""

import asyncio
from datetime import datetime, timedelta

import octopus as octopus_module
from octopus import OctopusAPI


def test_octopus_sensor_due_wrapper(my_predbat):
    """Wrapper to run the async sensor_due scheduling tests"""
    return asyncio.run(test_octopus_sensor_due(my_predbat))


class FakeDateTime(datetime):
    """datetime replacement whose now() returns a controllable fixed time"""

    current = None

    @classmethod
    def now(cls, tz=None):
        """Return the controlled current time"""
        return cls.current


async def run_at(api, fake_time, seconds):
    """Run the component loop body once at the given fake wall-clock time"""
    FakeDateTime.current = fake_time
    return await api.run(seconds, False)


async def test_octopus_sensor_due(my_predbat):
    """
    Test the sensor_due scheduling in OctopusAPI.run()

    Tests:
    - Test 1: Sensor updates must not starve when run() only lands on odd wall-clock minutes
    - Test 2: Dispatch fetch and sensor update happen on a 2 minute cadence based on the age of the last update
    """
    print("**** Running Octopus sensor_due scheduling tests ****")
    failed = False

    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    # Stub out the dispatch fetch and sensor publish so we only exercise the scheduling logic
    dispatch_fetches = []
    sensor_updates = []

    async def fake_update_devices(account_id):
        """Record a dispatch fetch instead of calling the API"""
        dispatch_fetches.append(FakeDateTime.current)

    async def fake_update_sensor(account_id):
        """Record a sensor update instead of publishing entities"""
        sensor_updates.append(FakeDateTime.current)

    api.async_update_intelligent_devices = fake_update_devices
    api.async_intelligent_update_sensor = fake_update_sensor

    # Control the wall clock seen by octopus.py
    saved_datetime = octopus_module.datetime
    octopus_module.datetime = FakeDateTime

    try:
        # Test 1: run() landing only on odd minutes must still update the sensor.
        # Simulates the drifted scheduler (e.g. a slow run() pushing the period to ~120s).
        print("\n*** Test 1: no starvation when runs land on odd minutes only ***")
        base = datetime(2024, 6, 15, 0, 1, 0)  # 00:01, an odd minute
        api.tariff_fetched_at = base
        api.device_fetched_at = base

        for step in range(3):
            # Runs at 00:01, 00:03 and 00:05 - all odd minutes
            await run_at(api, base + timedelta(minutes=2 * step), 60 * (step + 1))

        if not sensor_updates:
            print("ERROR: Sensor was never updated across 4 minutes of odd-minute runs")
            failed = True
        elif not dispatch_fetches:
            print("ERROR: Intelligent dispatches were never fetched across 4 minutes of odd-minute runs")
            failed = True
        else:
            print("PASS: Sensor updated {} times and dispatches fetched {} times across odd-minute runs".format(len(sensor_updates), len(dispatch_fetches)))

        # Test 2: updates follow a 2 minute cadence from the last update, not minute parity
        print("\n*** Test 2: 2 minute cadence based on last update age ***")
        sensor_updates.clear()
        dispatch_fetches.clear()
        t0 = datetime(2024, 6, 15, 1, 0, 0)
        api.tariff_fetched_at = t0
        api.device_fetched_at = t0
        api.sensor_updated_at = None

        await run_at(api, t0, 60)  # Never updated - must update now
        await run_at(api, t0 + timedelta(minutes=1), 120)  # Only 1 minute old - must not update
        await run_at(api, t0 + timedelta(minutes=2), 180)  # 2 minutes old - must update

        expected = [t0, t0 + timedelta(minutes=2)]
        if sensor_updates != expected:
            print("ERROR: Expected sensor updates at {}, got {}".format(expected, sensor_updates))
            failed = True
        elif dispatch_fetches != expected:
            print("ERROR: Expected dispatch fetches at {}, got {}".format(expected, dispatch_fetches))
            failed = True
        else:
            print("PASS: Dispatch fetch and sensor update follow the 2 minute cadence")
    finally:
        octopus_module.datetime = saved_datetime

    return failed
