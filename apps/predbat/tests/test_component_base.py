# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for ComponentBase start method and backoff behavior
"""

import asyncio
import sys
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from component_base import ComponentBase, new_http_session, set_http_session_factory, writes
from coordinator import inverter_record
from fox import FoxAPI
from hypervolt import ChargerLink, HypervoltAPI
from myenergi import MyEnergiAPI
from octopus import OctopusAPI, OctopusEnergyApiClient
from ohme import OhmeAPI
from solis import SolisAPI
from teslemetry import TeslemetryAPI
from wallbox import WallboxAPI

# Save original sleep before any patching
_original_sleep = asyncio.sleep


# Fast sleep function for tests - sleeps 1/100th of the specified time
async def fast_sleep(delay, result=None):
    """Sleep for 1/100th of the specified time for faster tests"""
    await _original_sleep(delay / 100, result)


class MockBase:
    """Mock base object for testing ComponentBase"""

    def __init__(self):
        self.log_messages = []
        self.local_tz = timezone.utc
        self.prefix = "predbat"
        self.args = {}
        self.had_errors = False
        self.fatal_error = False

    def log(self, message):
        """Mock log function"""
        self.log_messages.append(message)
        print(message)

    def call_notify(self, message):
        """Mock notify method"""
        self.log_messages.append("Alert: " + message)


class TestComponent(ComponentBase):
    """Test component implementation"""

    def __init__(self, base, fail_until_attempt=0, return_true_on_run=True, **kwargs):
        """
        Args:
            fail_until_attempt: Number of run() calls that should return False before succeeding
            return_true_on_run: Whether run() should return True or False after fail_until_attempt
        """
        self.run_count = 0
        self.fail_until_attempt = fail_until_attempt
        self.return_true_on_run = return_true_on_run
        super().__init__(base, **kwargs)

    def initialize(self, **kwargs):
        """Initialize test component"""
        pass

    async def run(self, seconds, first):
        """Mock run method"""
        self.run_count += 1
        self.log(f"TestComponent: run() called (attempt {self.run_count}, seconds={seconds}, first={first})")

        # Fail for the first N attempts
        if self.run_count <= self.fail_until_attempt:
            return False

        return self.return_true_on_run


def test_component_base_not_calculating(my_predbat):
    """Test that components are not calculating unless they explicitly say otherwise."""
    component = TestComponent(MockBase())
    assert not component.is_calculating(), "ComponentBase should default to not calculating"
    print("PASS: ComponentBase defaults to not calculating")
    return False


def test_component_base_immediate_success(my_predbat):
    """Test component that succeeds on first run"""
    print("\n*** Test: ComponentBase immediate success ***")

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = TestComponent(base, fail_until_attempt=0, return_true_on_run=True)

            # Start component in background
            task = asyncio.create_task(component.start())

            # Wait briefly for it to start (1.0 → 0.01s real via fast_sleep; must be
            # shorter than the component's 5s loop sleep → 0.05s real)
            await asyncio.sleep(1.0)

            # Check it started successfully
            assert component.api_started, "Component should have started"
            assert component.run_count == 1, f"Expected 1 run call, got {component.run_count}"

            # Stop component
            await component.stop()
            await task

            print("PASS: Component started immediately on first run")
        return False  # False = test passed (no failure)

    return asyncio.run(run_test())


def test_component_base_backoff_sequence(my_predbat):
    """Test component with backoff on failure"""
    print("\n*** Test: ComponentBase backoff sequence ***")

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = TestComponent(base, fail_until_attempt=1, return_true_on_run=True)

            # Start component in background
            task = asyncio.create_task(component.start())

            # First run happens immediately (at second 0)
            await asyncio.sleep(1.0)
            assert component.run_count == 1, f"Expected 1 run after start, got {component.run_count}"
            assert not component.api_started, "Component should not have started yet (failed first attempt)"

            # Wait slightly longer - should still be 1 run (waiting for backoff)
            # Backoff = 60s real → 0.6s real via fast_sleep; we wait 0.5 → 0.005s real
            await asyncio.sleep(0.5)
            assert component.run_count == 1, f"Should still be 1 run (waiting for backoff), got {component.run_count}"

            # Stop component before the backoff completes
            await component.stop()
            await task

            print(f"PASS: Component backoff working (run_count={component.run_count})")
        return False  # False = test passed

    return asyncio.run(run_test())


def test_component_base_stop_during_backoff(my_predbat):
    """Test that api_stop is respected during backoff period"""
    print("\n*** Test: ComponentBase respects api_stop during backoff ***")

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = TestComponent(base, fail_until_attempt=10, return_true_on_run=True)

            # Start component in background
            task = asyncio.create_task(component.start())

            # Wait for first run
            await asyncio.sleep(1.0)
            assert component.run_count == 1, f"Expected 1 run, got {component.run_count}"
            assert not component.api_started, "Component should not have started yet"

            # Stop component during backoff period
            await component.stop()
            await task

            # Verify it stopped cleanly without waiting for the full backoff
            assert not component.api_started, "Component should not have started"
            print(f"PASS: Component stopped during backoff (run_count={component.run_count})")
        return False  # False = test passed

    return asyncio.run(run_test())


def test_component_base_normal_operation_after_start(my_predbat):
    """Test that component runs every 60 seconds after successful start"""
    print("\n*** Test: ComponentBase normal operation after start ***")

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = TestComponent(base, fail_until_attempt=0, return_true_on_run=True)

            # Start component in background
            task = asyncio.create_task(component.start())

            # Wait for it to start
            await asyncio.sleep(1.0)
            assert component.api_started, "Component should have started"
            initial_run_count = component.run_count

            # Wait a bit more - should not run again immediately (only every 60 seconds)
            # Component loop sleep = 5s → 0.05s real; we wait 0.5 → 0.005s real
            await asyncio.sleep(0.5)
            assert component.run_count == initial_run_count, f"Should not run again immediately, expected {initial_run_count}, got {component.run_count}"

            # Stop component
            await component.stop()
            await task

            print(f"PASS: Component operates normally after start (run_count={component.run_count})")
        return False  # False = test passed

    return asyncio.run(run_test())


def test_component_base_exception_handling(my_predbat):
    """Test that exceptions during run() are handled with backoff"""
    print("\n*** Test: ComponentBase exception handling with backoff ***")

    class ExceptionComponent(ComponentBase):
        def __init__(self, base, fail_count=2):
            self.run_count = 0
            self.fail_count = fail_count
            super().__init__(base)

        def initialize(self, **kwargs):
            pass

        async def run(self, seconds, first):
            self.run_count += 1
            if self.run_count <= self.fail_count:
                raise Exception(f"Test exception {self.run_count}")
            return True

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = ExceptionComponent(base, fail_count=1)

            # Start component in background
            task = asyncio.create_task(component.start())

            # Wait for first run
            await asyncio.sleep(1.0)
            assert component.run_count == 1, f"Expected 1 run, got {component.run_count}"
            assert not component.api_started, "Component should not have started due to exception"
            assert component.count_errors == 1, "Error count should be incremented"

            # Check error was logged
            error_logged = any("Error:" in msg for msg in base.log_messages)
            assert error_logged, "Exception should have been logged"

            # Stop component
            await component.stop()
            await task

            print(f"PASS: Component handles exceptions with backoff (run_count={component.run_count}, errors={component.count_errors})")
        return False  # False = test passed

    return asyncio.run(run_test())


def test_component_base_run_timeout(my_predbat):
    """Test that a hung run() is detected, its stack is logged, and it is treated as a failure"""
    print("\n*** Test: ComponentBase run() timeout detection ***")

    class SlowComponent(ComponentBase):
        def __init__(self, base):
            self.run_count = 0
            super().__init__(base)
            self.run_timeout = 0.05  # 50 ms - fires before fast_sleep(10) completes (~100 ms real)

        def initialize(self, **kwargs):
            pass

        async def run(self, seconds, first):
            self.run_count += 1
            await asyncio.sleep(10)  # fast_sleep makes this ~100 ms real - longer than timeout
            return True

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = SlowComponent(base)

            task = asyncio.create_task(component.start())

            # Wait long enough for: timeout to fire + error processing + one sleep(5) cycle
            await asyncio.sleep(2)  # ~20 ms real time via fast_sleep - enough for timeout + bookkeeping

            await component.stop()
            await task

            # Component should not have started - run() never returned True
            assert not component.api_started, "Component should not have started (run timed out)"

            # Error count must have been incremented
            assert component.count_errors > 0, f"Error count should be > 0, got {component.count_errors}"

            # A 'timeout' message must appear in the log
            timeout_logged = any("timeout" in msg.lower() for msg in base.log_messages)
            assert timeout_logged, "Timeout should have been logged. Messages:\n" + "\n".join(base.log_messages)

            # A traceback line should also have been logged (stack dump)
            traceback_logged = any("File" in msg for msg in base.log_messages)
            assert traceback_logged, "Stack trace should have been logged. Messages:\n" + "\n".join(base.log_messages)

            print(f"PASS: Timeout caught and stack-traced (error_count={component.count_errors})")
        return False  # False = test passed

    return asyncio.run(run_test())


def test_component_base_first_cleared_when_run_presets_api_started(my_predbat):
    """Regression: a component that sets api_started itself must still leave the startup path.

    The gateway's MQTT background loop sets self.api_started = True before run(first=True)
    returns. If start() only clears the `first` flag inside `if not self.api_started`, the
    flag stays True forever and start() keeps re-running the first=True startup path on
    backoff, never reaching the steady-state (first=False) housekeeping that publishes the
    plan. This verifies start() transitions to first=False regardless of who set api_started.
    """
    print("\n*** Test: ComponentBase clears first when run() pre-sets api_started ***")

    class PresetComponent(ComponentBase):
        def __init__(self, base):
            self.first_flags = []
            super().__init__(base)

        def initialize(self, **kwargs):
            pass

        async def run(self, seconds, first):
            self.first_flags.append(first)
            # Mimic a background task marking the component started before run() returns.
            self.api_started = True
            return True

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = PresetComponent(base)

            task = asyncio.create_task(component.start())

            # Wait for the component loop to advance past simulated seconds=60 so a steady-state
            # run can occur - polled, so it ends as soon as one has (about 0.65s real via
            # fast_sleep) instead of always sitting out the full 200 simulated seconds.
            for _ in range(200):
                if any(flag is False for flag in component.first_flags):
                    break
                await asyncio.sleep(1)

            assert component.api_started, "Component should be started"

            await component.stop()
            await task

            assert component.first_flags, "run() should have been called"
            assert component.first_flags[0] is True, "First run should be first=True"
            assert any(f is False for f in component.first_flags), "Component must reach steady-state housekeeping (first=False); got first flags: {}".format(component.first_flags)
            assert component.first_flags.count(True) == 1, "Startup run() should happen exactly once, got {}".format(component.first_flags)

            print(f"PASS: first cleared despite self-set api_started (flags={component.first_flags})")
        return False  # False = test passed

    return asyncio.run(run_test())


def test_component_base_set_arg_auto(my_predbat):
    """
    Test ComponentBase.set_arg_auto() (issue #4494 follow-up, PR #4500 review): warns once when
    it overwrites a key the user had explicitly set in apps.yaml, otherwise behaves exactly like
    set_arg() - auto-discovery always wins either way, this only makes the override discoverable.
    """
    print("\n*** Test: ComponentBase.set_arg_auto warns once on apps.yaml override ***")

    base = MockBase()
    base.args_from_apps_yaml = {"battery_scaling": [0.9]}
    base.apps_yaml_override_warned = set()
    set_calls = {}
    base.set_arg = lambda arg, value: set_calls.__setitem__(arg, value)

    component = TestComponent(base)

    # apps.yaml had a different value - warn once, auto-discovered value still applied
    component.set_arg_auto("battery_scaling", ["sensor.predbat_battery_soh"])
    assert set_calls.get("battery_scaling") == ["sensor.predbat_battery_soh"], "Auto-discovered value should be applied"
    assert any("apps.yaml sets 'battery_scaling: [0.9]'" in msg for msg in base.log_messages), "Should warn about the override"

    # Second call for the same key must not repeat the warning
    component.set_arg_auto("battery_scaling", ["sensor.predbat_battery_soh"])
    warn_count = sum(1 for msg in base.log_messages if "apps.yaml sets 'battery_scaling" in msg)
    assert warn_count == 1, f"Warning should not repeat, got {warn_count}"

    # A key never present in apps.yaml at all - no warning, behaves like plain set_arg
    component.set_arg_auto("num_inverters", 1)
    assert set_calls.get("num_inverters") == 1, "Should still set the value for an unconfigured key"
    assert not any("num_inverters" in msg for msg in base.log_messages), "Should not warn for a key the user never configured"

    # Base with no args_from_apps_yaml snapshot at all (e.g. component created outside
    # PredBat.initialize(), as in most unit tests) must not raise, and must not warn
    bare_base = MockBase()
    bare_set_calls = {}
    bare_base.set_arg = lambda arg, value: bare_set_calls.__setitem__(arg, value)
    bare_component = TestComponent(bare_base)
    bare_component.set_arg_auto("battery_scaling", ["sensor.predbat_battery_soh"])
    assert bare_set_calls.get("battery_scaling") == ["sensor.predbat_battery_soh"], "Should still work without an apps_yaml snapshot"

    print("PASS: set_arg_auto warns once on a genuine override, stays silent otherwise, and is safe without a snapshot")
    return False


def test_component_base_set_arg_auto_keeps_user_setting(my_predbat):
    """
    Test ComponentBase.set_arg_auto(overwrite=False): leaves a key the user set in apps.yaml alone.

    Repointing an entity key at a freshly published sensor throws away that sensor's recorder
    history. For the keys Predbat reads history from - the daily energy totals its load model is
    built out of - that means planning against no history at all until days accumulate, so those
    callers opt out of overwriting. The default stays overwrite=True, which is what keeps every
    existing caller's behaviour unchanged.
    """
    print("\n*** Test: ComponentBase.set_arg_auto(overwrite=False) keeps the user's apps.yaml setting ***")

    base = MockBase()
    base.args_from_apps_yaml = {"load_today": ["sensor.my_own_load_today"]}
    base.apps_yaml_override_warned = set()
    set_calls = {}
    base.set_arg = lambda arg, value: set_calls.__setitem__(arg, value)

    component = TestComponent(base)

    # The user configured this key, so overwrite=False must not touch it at all
    component.set_arg_auto("load_today", ["sensor.predbat_givtcp_0_load_today"], overwrite=False)
    assert "load_today" not in set_calls, f"User's apps.yaml setting should not be overwritten, got {set_calls.get('load_today')}"
    assert any("load_today" in msg and "keeping your apps.yaml setting" in msg for msg in base.log_messages), "Should say the user's setting was kept"

    # Said once when it happens, not on every automatic_config pass for the life of the process
    component.set_arg_auto("load_today", ["sensor.predbat_givtcp_0_load_today"], overwrite=False)
    kept_count = sum(1 for msg in base.log_messages if "load_today" in msg and "keeping your apps.yaml setting" in msg)
    assert kept_count == 1, f"Kept message should not repeat, got {kept_count}"

    # A key the user never configured is still auto-discovered - overwrite=False protects the
    # user's own value, it does not stop auto-discovery filling in a key that has none
    component.set_arg_auto("pv_today", ["sensor.predbat_givtcp_0_pv_today"], overwrite=False)
    assert set_calls.get("pv_today") == ["sensor.predbat_givtcp_0_pv_today"], "An unconfigured key should still be auto-configured"

    # The default is unchanged: auto-discovery still wins when overwrite is not passed
    component.set_arg_auto("load_today", ["sensor.predbat_givtcp_0_load_today"])
    assert set_calls.get("load_today") == ["sensor.predbat_givtcp_0_load_today"], "Default overwrite=True should still apply the auto-discovered value"

    # Keeping the user's value must not depend on the warned-set bookkeeping being present -
    # a component built outside PredBat.initialize() has neither snapshot attribute
    bare_base = MockBase()
    bare_base.args_from_apps_yaml = {"load_today": ["sensor.my_own_load_today"]}
    bare_set_calls = {}
    bare_base.set_arg = lambda arg, value: bare_set_calls.__setitem__(arg, value)
    bare_component = TestComponent(bare_base)
    bare_component.set_arg_auto("load_today", ["sensor.predbat_givtcp_0_load_today"], overwrite=False)
    assert "load_today" not in bare_set_calls, "User's setting should be kept even without an apps_yaml_override_warned set"

    print("PASS: set_arg_auto(overwrite=False) keeps an explicit apps.yaml setting and still fills in unset keys")


def test_component_base_set_state_external(my_predbat):
    """
    Test ComponentBase.set_state_external() forwards to the HA interface with the attributes intact.

    Components use this (rather than set_state_wrapper) when auto-discovery has to change one of
    Predbat's own settings - only this path updates the matching CONFIG_ITEMS value, so writing the
    state alone would move the displayed entity without changing what the planner reads.
    """
    print("\n*** Test: ComponentBase.set_state_external forwards to the HA interface ***")

    calls = []

    async def capture(entity_id, state, attributes=None):
        """Record a forwarded external state write."""
        if attributes is None:
            attributes = {}
        calls.append((entity_id, state, attributes))
        return "written"

    base = MockBase()
    base.ha_interface = SimpleNamespace(set_state_external=capture)
    component = TestComponent(base)

    result = asyncio.run(component.set_state_external("switch.predbat_inverter_hybrid", False))
    assert calls == [("switch.predbat_inverter_hybrid", False, {})], f"Unexpected forwarded call {calls}"
    assert result == "written", "The HA interface's return value should be passed back to the caller"

    asyncio.run(component.set_state_external("sensor.predbat_test", 42, {"unit_of_measurement": "W"}))
    assert calls[1] == ("sensor.predbat_test", 42, {"unit_of_measurement": "W"}), f"Attributes not forwarded: {calls[1]}"

    print("PASS: set_state_external forwards entity, state and attributes and returns the result")
    return False


def test_component_base_midnight_utc_ignores_rewound_base(my_predbat):
    """
    Test ComponentBase.midnight_utc ignores a base.midnight_utc rewound by calculate_yesterday (GH#4804).

    calculate_yesterday() (output.py) rewinds the shared base.midnight_utc by a day for the duration
    of the savings calculation and restores it ~350 lines later. Components run on their own OS
    threads (hass.py's create_task uses threading.Thread) on schedules of their own, so a component
    reading the passthrough mid-rewind used to see yesterday's midnight - which bucketed a whole PV
    forecast one day late, emptying "today" (the reported incident), and can shift any other
    component's day arithmetic the same way.

    The property therefore derives today's midnight from base.now_utc, the field calculate_yesterday
    never fakes. On a healthy base the two are identical: predbat.py's update_time() sets
    midnight_utc = now_utc.replace(hour=0, ...), so this changes nothing outside the rewind window.
    """
    print("\n*** Test: ComponentBase.midnight_utc ignores a rewound base.midnight_utc ***")

    base = MockBase()
    base.now_utc = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    base.midnight_utc = base.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    component = TestComponent(base)

    assert component.midnight_utc == datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc), f"Healthy base should give today's midnight, got {component.midnight_utc}"

    # A concurrent calculate_yesterday() is mid-flight: the shared field points at yesterday.
    base.midnight_utc = base.midnight_utc - timedelta(days=1)
    assert component.midnight_utc == datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc), f"Rewound base.midnight_utc leaked into the component: {component.midnight_utc}"

    print("PASS: midnight_utc derives from now_utc and ignores the rewound shared value")
    return False


def test_component_base_minutes_now_follows_update_time(my_predbat):
    """
    Test ComponentBase.minutes_now matches update_time()'s own value and ignores the faked one (GH#4804).

    calculate_yesterday() (output.py) fakes base.minutes_now to 0 alongside its midnight_utc rewind.
    0 is the nastier of the two, since it reads as a legitimate "just after midnight" rather than an
    obviously wrong date - it would tell octopus.py to keep every expired dispatch slot, and the
    AlphaESS/Deye/Sunsynk adapters that pick the live TOU slot by time of day to believe it is
    midnight while writing to a real inverter.

    The derived value has to agree with update_time()'s (predbat.py), including its PREDICT_STEP
    flooring - so the first half of this test compares the two on the real base rather than
    restating the formula, and would catch the two drifting apart later.
    """
    print("\n*** Test: ComponentBase.minutes_now follows update_time and ignores the faked value ***")

    # The fixture pins its own clock (unit_test.py's create_predbat) and the whole suite shares this
    # instance, so every field update_time() writes has to go back - not just the ones read here, or
    # a later test inherits this one's wall-clock state.
    saved = {name: getattr(my_predbat, name) for name in ("now_utc", "now_utc_real", "midnight_utc", "minutes_now", "minutes_to_midnight", "difference_minutes", "local_tz")}
    try:
        my_predbat.update_time(print=False)
        component = TestComponent(my_predbat)
        assert component.minutes_now == my_predbat.minutes_now, f"Derived {component.minutes_now} but update_time() computed {my_predbat.minutes_now}"
        assert component.minutes_now % 5 == 0, f"Should stay on a PREDICT_STEP boundary, got {component.minutes_now}"
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)

    base = MockBase()
    base.now_utc = datetime(2025, 6, 15, 14, 37, 0, tzinfo=timezone.utc)
    base.midnight_utc = base.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    base.minutes_now = 14 * 60 + 35
    component = TestComponent(base)

    assert component.minutes_now == 14 * 60 + 35, f"Expected 14:35 floored to a PREDICT_STEP boundary, got {component.minutes_now}"

    # A concurrent calculate_yesterday() is mid-flight: the shared fields say midnight yesterday.
    base.minutes_now = 0
    base.midnight_utc = base.midnight_utc - timedelta(days=1)
    assert component.minutes_now == 14 * 60 + 35, f"Faked base.minutes_now leaked into the component: {component.minutes_now}"

    print("PASS: minutes_now follows update_time's computation and ignores the faked value")
    return False


def test_component_base_minutes_now_snapshots_the_clock(my_predbat):
    """
    Test minutes_now survives update_time() landing mid-read.

    The value needs now_utc and the midnight derived from it, and self.midnight_utc goes back to
    base.now_utc for its own read - so a naive implementation reads the field twice. update_time()
    runs on the main thread while components run on theirs, so those two reads can straddle it: at
    a day boundary that subtracts the new day's midnight from the old day's timestamp and collapses
    23:59 to 0, which reads as midnight rather than as an error.

    A base whose now_utc advances a day between reads reproduces that deterministically, without
    needing two threads to interleave.
    """
    print("\n*** Test: ComponentBase.minutes_now snapshots now_utc ***")

    class RollingClockBase(MockBase):
        """A base whose clock rolls into the next day between one read of now_utc and the next."""

        def __init__(self):
            """Start the day before, with a read counter."""
            super().__init__()
            self.reads = 0

        @property
        def now_utc(self):
            """Return 23:59 on the first read and the next day's noon on every read after it."""
            self.reads += 1
            if self.reads == 1:
                return datetime(2025, 6, 15, 23, 59, 0, tzinfo=timezone.utc)
            return datetime(2025, 6, 16, 12, 0, 0, tzinfo=timezone.utc)

    base = RollingClockBase()
    component = TestComponent(base)

    minutes_now = component.minutes_now
    assert base.reads == 1, f"now_utc should be read once per call, was read {base.reads} times"
    assert minutes_now == 23 * 60 + 55, f"Expected the first read's 23:59 floored to 23:55, got {minutes_now}"

    print("PASS: minutes_now reads the clock once and stays on that reading")
    return False


# ============================================================================
# refresh_discovery() / discovery_entities() - the shared discovery reporting loop
#
# Every reporter (GivTCP, GE Cloud, Octopus, Ohme, Solcast) drives its report through these two
# methods rather than hand-rolling the loop, so the rules they used to each restate in prose are
# pinned once, here. See ComponentBase.refresh_discovery()'s own docstring for why each holds.
# ============================================================================


class _DiscoveryCoordinator:
    """Stand-in for coordinator.Coordinator: records what each component filed."""

    def __init__(self):
        """Start with nothing filed."""
        self.filed = []

    def report(self, component_name, report):
        """Record one component's report, as the real coordinator would."""
        self.filed.append((component_name, report))


class _DiscoveryComponent(ComponentBase):
    """A component that reports whatever is put in self.next_report."""

    def initialize(self, **kwargs):
        """Start with an empty report and no recorded build calls."""
        self.next_report = {"inverters": []}
        self.build_calls = 0
        self.build_raises = False

    async def run(self, seconds, first):
        """Never driven directly by these tests."""
        return True

    def build_discovery(self):
        """Return the report this test staged, or blow up if it asked for a failure."""
        self.build_calls += 1
        if self.build_raises:
            raise RuntimeError("boom")
        return self.next_report


def _discovery_component():
    """One component wired to a recording coordinator, returned with it."""
    base = MockBase()
    coordinator = _DiscoveryCoordinator()
    base.components = SimpleNamespace(coordinator=coordinator)
    component = _DiscoveryComponent(base)
    component.component_name = "test_component"
    return component, coordinator


def test_component_base_refresh_discovery_files_and_stops_churning(my_predbat):
    """A changed report is filed; an unchanged one is not re-filed."""
    component, coordinator = _discovery_component()
    component.next_report = {"inverters": [{"device_id": "test:1"}]}

    component.refresh_discovery()
    assert len(coordinator.filed) == 1, f"The first report should be filed, got {len(coordinator.filed)}"
    assert coordinator.filed[0][0] == "test_component", "The report should be filed under the component's registry name"

    component.refresh_discovery()
    assert len(coordinator.filed) == 1, "An unchanged report must not be re-filed - that would churn the catalogue every cycle"

    component.next_report = {"inverters": [{"device_id": "test:1", "serials": ["SERIAL-1"]}]}
    component.refresh_discovery()
    assert len(coordinator.filed) == 2, "A report that has moved on should replace the last one"
    print("PASS: refresh_discovery files a changed report and stops churning on an unchanged one")
    return False


def test_component_base_refresh_discovery_skips_none_and_missing_build(my_predbat):
    """A None report files nothing, and a component with no build_discovery() is a no-op."""
    component, coordinator = _discovery_component()
    component.next_report = None

    component.refresh_discovery()
    assert coordinator.filed == [], "A None report means 'nothing to describe yet' and must file nothing"
    assert component._discovery_report is None, "A None report must not advance the marker"

    component.next_report = {"inverters": [{"device_id": "test:1"}]}
    component.refresh_discovery()
    assert len(coordinator.filed) == 1, "Once there is something to describe it should file"

    plain = TestComponent(MockBase())
    plain.refresh_discovery()  # no build_discovery() at all - must not raise
    print("PASS: refresh_discovery skips a None report and a component that does not report at all")
    return False


def test_component_base_refresh_discovery_failure_is_contained_and_retried(my_predbat):
    """A build failure is logged, never raised, and the marker is left for the next cycle to retry."""
    component, coordinator = _discovery_component()
    component.next_report = {"inverters": [{"device_id": "test:1"}]}
    component.build_raises = True

    component.refresh_discovery()  # must not raise - an observer may not degrade what it observes

    assert coordinator.filed == [], "Nothing should be filed when the build failed"
    assert component._discovery_report is None, "A failed report must not advance the marker"
    assert not component.base.had_errors, "A discovery failure must never set had_errors - that would suppress record_status()"
    assert any("failed to report discovery" in message for message in component.base.log_messages), "The failure should be logged"

    component.build_raises = False
    component.refresh_discovery()
    assert len(coordinator.filed) == 1, "The next cycle should retry and succeed, not stay lost for the life of the process"
    print("PASS: a build_discovery failure is contained, logged and retried rather than lost")
    return False


def test_component_base_refresh_discovery_marker_waits_for_the_report_to_land(my_predbat):
    """The marker advances only after the report is filed, not merely after it is built.

    If it advanced first, a coordinator that threw would leave the component believing it had
    reported - and since the rebuilt report would then match the marker every cycle, that report
    would be lost for the life of the process. The order in refresh_discovery() is what prevents
    it, so pin it here rather than leave it to reading.
    """
    component, coordinator = _discovery_component()
    component.next_report = {"inverters": [{"device_id": "test:1"}]}

    def _explode(report):
        raise RuntimeError("coordinator is down")

    component.report_discovery = _explode
    component.refresh_discovery()  # must not raise

    assert component._discovery_report is None, "The marker must not advance when filing the report failed"

    component.report_discovery = lambda report: coordinator.report(component.component_name, report)
    component.refresh_discovery()
    assert len(coordinator.filed) == 1, "The next cycle should retry the report the coordinator rejected"
    print("PASS: the marker waits for the report to actually land, so a rejected report is retried")
    return False


def test_component_base_refresh_discovery_survives_a_component_built_without_init(my_predbat):
    """A component built with Cls.__new__(Cls) must not raise out of refresh_discovery().

    The test harnesses across this repo construct components that way to exercise one method in
    isolation (solcast's own tests use SolarAPI.__new__(SolarAPI)), so component_name and the
    report marker are declared on the class rather than only assigned in __init__. Without that,
    the failure path itself raised AttributeError - which escapes run() and is exactly the
    degradation the guard exists to prevent.
    """
    component = _DiscoveryComponent.__new__(_DiscoveryComponent)
    base = MockBase()
    component.base = base
    component.log = base.log
    component.build_raises = True
    component.build_calls = 0

    component.refresh_discovery()  # must not raise, despite __init__ never having run

    assert component._discovery_report is None, "The class-level marker default should read as None"
    assert any("failed to report discovery" in message for message in base.log_messages), "The failure should still be logged, under the class name"
    print("PASS: refresh_discovery does not raise on a component built without __init__")
    return False


def test_component_base_refresh_discovery_refiles_when_live_state_behind_inverter_record_grows(my_predbat):
    """A report built with inverter_record() from live component state is re-filed when that state grows.

    refresh_discovery() stores the report it filed and compares each rebuild against it with ==.
    A reporter passing its own long-lived list (serials=self.serials) is the natural way to write
    one, so the builder must not let that list into the stored report: if it did, appending to it
    would change the stored report too, the rebuild would compare equal, and the grown fleet
    would never reach the catalogue - the report frozen at its first state.
    """
    component, coordinator = _discovery_component()
    component.fronted = ["battery001"]
    component.build_discovery = lambda: {"inverters": [inverter_record("test:gateway", composition="gateway", serials=component.fronted)]}

    component.refresh_discovery()
    assert len(coordinator.filed) == 1, f"The first report should be filed, got {len(coordinator.filed)}"

    component.fronted.append("battery002")
    component.refresh_discovery()
    assert len(coordinator.filed) == 2, "A grown fleet must be re-filed - the stored report must not have grown along with the live list"
    assert coordinator.filed[1][1]["inverters"][0]["serials"] == ["battery001", "battery002"], coordinator.filed[1][1]
    print("PASS: refresh_discovery re-files a report once the live state behind inverter_record() grows")
    return False


def test_component_base_discovery_entities_keeps_only_what_exists(my_predbat):
    """Only entities Home Assistant has actually seen survive - a spec is not evidence of publication."""
    component, _coordinator = _discovery_component()
    published = {"sensor.predbat_test_published": "5.0"}
    component.base.get_state_wrapper = lambda entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False: published.get(entity_id)

    entities = component.discovery_entities(
        {
            "charge_rate": {"entity_id": "sensor.predbat_test_published", "domain": "sensor", "access": "rw"},
            "discharge_rate": {"entity_id": "sensor.predbat_test_never_published", "domain": "sensor", "access": "rw"},
        }
    )

    assert set(entities) == {"charge_rate"}, f"Only the published entity should survive, got {list(entities)}"
    assert entities["charge_rate"]["domain"] == "sensor", "The descriptor should be carried through intact"
    print("PASS: discovery_entities keeps only the entities that exist in the state store")
    return False


def test_component_base_request_replan(my_predbat):
    """
    ComponentBase.request_replan() is how a component asks for the plan to be recomputed on the next
    15 second tick, rather than a component reaching into base.update_pending itself. It sets only
    update_pending - not plan_valid - so the recompute still weighs the current plan against the new one.
    """
    print("\n*** Test: ComponentBase.request_replan sets update_pending and logs why ***")
    base = MockBase()
    base.update_pending = False
    base.plan_valid = True
    component = TestComponent(base)

    component.request_replan("dispatches changed")

    assert base.update_pending is True, "request_replan should set update_pending"
    assert base.plan_valid is True, "request_replan must not invalidate the current plan"
    assert any("TestComponent" in msg and "dispatches changed" in msg for msg in base.log_messages), "request_replan should log the component and reason, got {}".format(base.log_messages)
    print("PASS: request_replan sets update_pending, leaves plan_valid and logs the reason")
    return False


class _LoopThread:
    """An event loop running on a thread of its own, standing in for a started component's loop."""

    def __init__(self):
        """Start the loop and wait until it is running."""
        import threading

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        while not self.loop.is_running():
            time.sleep(0.01)

    def stop(self):
        """Stop the loop and its thread, leaving the loop closed."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if not self.loop.is_closed():
            self.loop.close()


def test_component_base_on_component_loop(my_predbat):
    """
    on_component_loop() runs a coroutine on the loop the component's start() is running on, however
    the caller got here. Entity write events are awaited from other event loops, and anything bound
    to the component's loop (an HTTP session, a lock) only works on that one.
    """
    print("\n*** Test: ComponentBase.on_component_loop hands work over to the component's loop ***")
    import threading

    base = MockBase()
    component = TestComponent(base)

    async def where(value):
        """Report the thread this ran on."""
        return value, threading.get_ident()

    async def boom():
        """Fail on whichever loop runs this."""
        raise ValueError("boom")

    # Not started: there is no component loop, so the work runs where it was called
    assert component.component_loop is None, "a component that has not started has no loop"
    assert asyncio.run(component.on_component_loop(where(1), timeout=5)) == (1, threading.get_ident()), "with no component loop the coroutine runs in place"

    worker = _LoopThread()
    try:
        component.component_loop = worker.loop

        # From another loop: handed over, result returned, exception propagated
        value, ident = asyncio.run(component.on_component_loop(where(3), timeout=5))
        assert value == 3 and ident == worker.thread.ident, "the coroutine must run on the component's loop thread, ran on {}".format(ident)
        try:
            asyncio.run(component.on_component_loop(boom(), timeout=5))
            raise AssertionError("an exception on the component's loop must reach the caller")
        except ValueError:
            pass

        # The work's own TimeoutError is its exception, not the hand-over's wait running out: it reaches
        # the caller at once, rather than being retried in a busy loop until the deadline (Python 3.11
        # made asyncio.TimeoutError the built-in TimeoutError, so the two are the same class)
        async def times_out():
            """Fail with a TimeoutError of its own, as a request inside the work might."""
            raise TimeoutError("the work's own timeout")

        base.log_messages.clear()
        started = time.monotonic()
        cpu_started = time.process_time()
        try:
            asyncio.run(component.on_component_loop(times_out(), timeout=2, poll=0.5))
            raise AssertionError("a TimeoutError raised by the work must reach the caller")
        except TimeoutError as error:
            assert "the work's own timeout" in str(error), "the caller must get the work's own exception, got {!r}".format(error)
        waited = time.monotonic() - started
        assert waited < 0.5, "the work's TimeoutError must be raised at once, not after the wait ran out: {:.2f}s".format(waited)
        assert time.process_time() - cpu_started < 0.5, "the caller must not spin while the work's exception is pending"
        assert not any("still running" in msg for msg in base.log_messages), "a finished piece of work must not be reported as still running: {}".format(base.log_messages)

        # Already on the component's loop: awaited directly, not posted to itself (which would deadlock)
        value, ident = asyncio.run_coroutine_threadsafe(component.on_component_loop(where(4), timeout=5), worker.loop).result(5)
        assert value == 4 and ident == worker.thread.ident, "on its own loop the coroutine is awaited in place"

        # Slower than the caller will wait: the caller gets False, the work is not cancelled
        finished = threading.Event()

        async def slow():
            """Outlast the caller's wait, then record that it still completed."""
            await asyncio.sleep(0.3)
            finished.set()
            return "late"

        base.log_messages.clear()
        result = asyncio.run(component.on_component_loop(slow(), timeout=0.05, what="GivTCP write"))
        assert result is False, "a hand-over that outlasts the timeout returns False, got {}".format(result)
        assert any("GivTCP write" in msg for msg in base.log_messages), "the abandoned wait is logged with what it was, got {}".format(base.log_messages)
        assert finished.wait(5), "giving up the wait must not cancel the work on the component's loop"

        # The loop stops under a waiting caller: the wait is abandoned rather than sat out
        async def never():
            """Wait far longer than the test will."""
            await asyncio.sleep(60)

        async def stop_under_caller():
            """Start a hand-over, then stop the component's loop while it is waited for."""
            waiter = asyncio.ensure_future(component.on_component_loop(never(), timeout=30, poll=0.05))
            await asyncio.sleep(0.1)
            worker.loop.call_soon_threadsafe(worker.loop.stop)
            return await asyncio.wait_for(waiter, timeout=5)

        assert asyncio.run(stop_under_caller()) is False, "a hand-over whose loop stopped returns False"
    finally:
        worker.stop()

    # A stopped (closed) loop is not handed to: the work runs in place again
    assert asyncio.run(component.on_component_loop(where(5), timeout=5)) == (5, threading.get_ident()), "with the component's loop closed the coroutine runs in place"

    # start() records the loop it runs on
    started = TestComponent(MockBase())

    async def run_briefly():
        """Run start() until its first tick, then stop it."""
        task = asyncio.ensure_future(started.start())
        await asyncio.sleep(0.05)
        loop = started.component_loop
        started.api_stop = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return loop is asyncio.get_running_loop()

    assert asyncio.run(run_briefly()), "start() must record the loop it is running on as component_loop"
    print("PASS: on_component_loop runs in place or hands over, and survives timeouts and a stopped loop")
    return False


def test_component_base_http_session_default(my_predbat):
    """
    With no factory installed, http_session() returns a new aiohttp.ClientSession built from the
    caller's kwargs, which the caller owns and closes - exactly what each component did itself.
    """
    print("\n*** Test: ComponentBase.http_session defaults to a new aiohttp.ClientSession ***")
    component = TestComponent(MockBase())

    async def build():
        timeout = aiohttp.ClientTimeout(total=12)
        first = component.http_session(timeout=timeout, headers={"X-Test": "1"})
        second = component.http_session()
        try:
            assert isinstance(first, aiohttp.ClientSession), "default http_session should be an aiohttp.ClientSession, got {}".format(type(first))
            assert first is not second, "each call should return a new session"
            assert first.timeout.total == 12, "timeout kwarg should reach the session"
            assert first.headers.get("X-Test") == "1", "headers kwarg should reach the session"
        finally:
            await first.close()
            await second.close()
        assert first.closed, "the caller closes the session it was given"

    asyncio.run(build())

    # Code that patches aiohttp.ClientSession (as many component tests do) must still take effect
    with patch("aiohttp.ClientSession", return_value="patched") as mock_session:
        assert component.http_session(timeout="t") == "patched", "a patched aiohttp.ClientSession should still be used"
        mock_session.assert_called_once_with(timeout="t")
    print("PASS: http_session returns a caller-owned aiohttp.ClientSession by default")
    return False


def test_component_base_http_session_factory(my_predbat):
    """
    A factory installed with set_http_session_factory() is used by every component's http_session()
    and by new_http_session() (mixins and API clients), with the caller's kwargs passed through.
    None restores the default.
    """
    print("\n*** Test: an installed HTTP session factory is used ***")
    calls = []
    sentinel = object()

    def factory(**kwargs):
        calls.append(kwargs)
        return sentinel

    component = TestComponent(MockBase())
    set_http_session_factory(factory)
    try:
        with patch("aiohttp.ClientSession", side_effect=AssertionError("default session must not be built while a factory is set")):
            assert component.http_session(timeout="t", headers={"a": "b"}) is sentinel, "http_session should return the factory's session"
            assert new_http_session(skip_auto_headers=["User-Agent"]) is sentinel, "new_http_session should return the factory's session"
            # A real component, built without __init__ as the component tests do, gets it too
            fox = FoxAPI.__new__(FoxAPI)
            assert fox.http_session() is sentinel, "a component subclass should use the factory"
    finally:
        set_http_session_factory(None)

    assert calls == [{"timeout": "t", "headers": {"a": "b"}}, {"skip_auto_headers": ["User-Agent"]}, {}], "kwargs should pass through unchanged, got {}".format(calls)
    with patch("aiohttp.ClientSession", return_value="default") as mock_session:
        assert component.http_session() == "default", "set_http_session_factory(None) should restore the default"
        mock_session.assert_called_once_with()
    print("PASS: the installed factory is used, and None restores the default")
    return False


class FakeResponse:
    """Async context manager standing in for an aiohttp response; every reply carries the same JSON."""

    status = 200
    headers = {}
    payload = {"idToken": "id", "refreshToken": "refresh", "id_token": "id2", "refresh_token": "refresh2", "success": True, "access_token": "new-token", "expires_at": "2099-01-01T00:00:00Z", "ok": 1}

    def __init__(self, text=""):
        self.body_text = text

    async def __aenter__(self):
        await asyncio.sleep(0)  # yield so that concurrent callers really interleave
        return self

    async def __aexit__(self, *exc):
        return None

    async def json(self, **kwargs):
        return dict(self.payload)

    async def text(self):
        return self.body_text


class FakeSession:
    """Records the requests made on it and whether it has been closed."""

    def __init__(self, owner, kwargs, text=""):
        self.owner = owner
        self.kwargs = kwargs
        self.text = text
        self.requests = []
        self.sockets = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        self.closed = True

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return FakeResponse(self.text)

    def ws_connect(self, url, **kwargs):
        self.sockets.append(url)
        return FakeWebSocket()

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class FakeWebSocket:
    """Async context manager standing in for a websocket that is closed as soon as it is read."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def receive(self):
        return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)


class FakeSessionFactory:
    """A session factory that remembers every session it hands out."""

    def __init__(self, name, text=""):
        self.name = name
        self.text = text
        self.sessions = []

    def __call__(self, **kwargs):
        session = FakeSession(self.name, kwargs, self.text)
        self.sessions.append(session)
        return session


class FactoryBase(MockBase):
    """MockBase for one PredBat instance, optionally carrying its own session factory."""

    def __init__(self, factory=None):
        super().__init__()
        self.args = {"user_id": "instance-" + (factory.name if factory else "none")}
        if factory is not None:
            self.http_session_factory = factory


def test_component_base_http_session_resolution_order(my_predbat):
    """
    http_session() picks base.http_session_factory first, then the global factory, then
    aiohttp.ClientSession looked up at call time. The base attribute is read on every call, and a
    component built with Cls.__new__ (no base) falls back to the global path.
    """
    print("\n*** Test: http_session resolution order ***")
    base_factory = FakeSessionFactory("base")
    global_factory = FakeSessionFactory("global")

    # Nothing set anywhere: the default is aiohttp.ClientSession, observed by patching it
    component = TestComponent(FactoryBase())
    with patch("aiohttp.ClientSession", return_value="default") as mock_session:
        assert component.http_session(timeout="t") == "default", "no factory anywhere should build an aiohttp.ClientSession"
        mock_session.assert_called_once_with(timeout="t")

    set_http_session_factory(global_factory)
    try:
        # The global factory is used when the base has none
        assert component.http_session(a=1).owner == "global", "the global factory should be used when the base has none"
        component.base.http_session_factory = None
        assert component.http_session().owner == "global", "a base factory of None should fall through to the global factory"

        # The base factory beats the global one, with the caller's kwargs passed through
        component.base.http_session_factory = base_factory
        session = component.http_session(timeout="t")
        assert session.owner == "base" and session.kwargs == {"timeout": "t"}, "the base factory should win and get the kwargs"

        # Read at call time, not cached when the component was built
        component.base.http_session_factory = None
        assert component.http_session().owner == "global", "removing the base factory should take effect on the next call"

        # No base at all (built with Cls.__new__ as the component tests do)
        bare = FoxAPI.__new__(FoxAPI)
        assert bare.http_session().owner == "global", "a component without a base should use the global factory"
    finally:
        set_http_session_factory(None)

    assert [s.owner for s in base_factory.sessions] == ["base"], "the base factory should only have been asked once"
    with patch("aiohttp.ClientSession", return_value="default") as mock_session:
        assert FoxAPI.__new__(FoxAPI).http_session() == "default", "a component without a base should default to aiohttp.ClientSession"
    print("PASS: base factory, then global factory, then aiohttp.ClientSession")
    return False


def _build_instance_components(base):
    """Build the real provider components for one fake PredBat instance."""
    ohme = OhmeAPI(base, email="driver@example.com", password="secret")
    hypervolt = HypervoltAPI(base, username="driver", password="secret")
    wallbox = WallboxAPI(base, username="driver", password="secret")
    myenergi = MyEnergiAPI(base, auth_method="oauth", key="old-token", token_hash="hash")
    return ohme, hypervolt, wallbox, myenergi


async def _use_instance_components(components):
    """Drive the real session-using code of each provider component, concurrently."""
    ohme, hypervolt, wallbox, myenergi = components
    results = await asyncio.gather(
        ohme.client.async_login(),
        hypervolt.transport._call("GET", "https://hypervolt.example/api"),
        wallbox.transport._call("GET", "https://wallbox.example/api", {}),
        myenergi.transport._request("GET", "/devices"),
        myenergi.check_and_refresh_oauth_token(),  # OAuthMixin refresh path
    )
    # The Octopus client is handed its component's factory the same way
    octopus_session = await OctopusEnergyApiClient("key", print, session_factory=myenergi.http_session).async_create_client_session()
    return results, octopus_session


def test_component_base_http_session_isolated_per_instance(my_predbat):
    """
    Two instances in one process, each with its own base factory, run real provider code
    concurrently. Every session comes from the factory of the instance that owns the component,
    including those made by the transports, API clients and the OAuth mixin, and a global factory
    installed as a trap is never used.
    """
    print("\n*** Test: sessions stay with the instance that owns the component ***")
    factory_a = FakeSessionFactory("A")
    factory_b = FakeSessionFactory("B")
    trap = FakeSessionFactory("global")
    comps_a = _build_instance_components(FactoryBase(factory_a))
    comps_b = _build_instance_components(FactoryBase(factory_b))

    async def run_both():
        return await asyncio.gather(_use_instance_components(comps_a), _use_instance_components(comps_b))

    set_http_session_factory(trap)
    try:
        with patch.dict("os.environ", {"SUPABASE_URL": "https://supabase.example", "SUPABASE_KEY": "key"}):
            with patch("aiohttp.ClientSession", side_effect=AssertionError("the default session must not be built")):
                (results_a, octopus_a), (results_b, octopus_b) = asyncio.run(run_both())
    finally:
        set_http_session_factory(None)

    assert trap.sessions == [], "the global factory must not be used when the base has its own"
    for name, factory, results, octopus in (("A", factory_a, results_a, octopus_a), ("B", factory_b, results_b, octopus_b)):
        assert results[0] is True, "{} ohme login should succeed through the fake session".format(name)
        assert results[4] is True, "{} oauth refresh should succeed through the fake session".format(name)
        assert octopus.owner == name, "{} octopus client session should come from its own factory".format(name)
        # Ohme login, Hypervolt call, Wallbox call, myenergi request, OAuth refresh, Octopus client
        assert len(factory.sessions) == 6, "{} should have made six sessions, got {}".format(name, len(factory.sessions))
        assert all(s.owner == name for s in factory.sessions), "{} only gets its own sessions".format(name)
        urls = sorted(url for s in factory.sessions for _, url in s.requests)
        assert any("verifyPassword" in url for url in urls), "{} ohme login should have posted".format(name)
        assert any("oauth-refresh" in url for url in urls), "{} oauth refresh should have posted".format(name)
        assert any("hypervolt.example" in url for url in urls) and any("wallbox.example" in url for url in urls), "{} transports should have called".format(name)
    assert comps_a[3].access_token == "new-token" and comps_b[3].access_token == "new-token", "both refreshed tokens applied"
    print("PASS: each instance's components only ever got sessions from their own factory")
    return False


def test_component_base_http_session_close_isolation(my_predbat):
    """
    Closing one instance's session leaves the other's open, and an exception raised inside
    `async with self.http_session()` still closes the session and propagates.
    """
    print("\n*** Test: session close isolation and close on exception ***")
    factory_a = FakeSessionFactory("A")
    factory_b = FakeSessionFactory("B")
    comp_a = TestComponent(FactoryBase(factory_a))
    comp_b = TestComponent(FactoryBase(factory_b))

    async def scenario():
        session_a = comp_a.http_session()
        session_b = comp_b.http_session()
        await session_a.close()
        assert session_a.closed and not session_b.closed, "closing A's session must leave B's open"
        await session_b.close()

        raised = None
        try:
            async with comp_a.http_session() as session:
                raise ValueError("boom")
        except ValueError as exc:
            raised = exc
        assert raised is not None, "the exception should propagate out of async with"
        assert session.closed, "the session must be closed when the body raises"
        return session

    asyncio.run(scenario())
    assert len(factory_a.sessions) == 2 and len(factory_b.sessions) == 1, "each factory only served its own component"
    print("PASS: close is per session, and an exception still closes it")
    return False


def _assert_only_base_factory(label, factory, trap, expect_sessions=1):
    """Assert the base factory (not the global trap) supplied the sessions a code path made."""
    assert trap.sessions == [], "{}: the global factory supplied a session, so the path ignored the base factory".format(label)
    assert len(factory.sessions) == expect_sessions, "{}: expected {} session(s) from the base factory, got {}".format(label, expect_sessions, len(factory.sessions))
    return factory.sessions


def _run_with_global_trap(trap, coroutine):
    """Run a coroutine with a global factory installed as a trap and the default session made fatal."""
    set_http_session_factory(trap)
    try:
        with patch("aiohttp.ClientSession", side_effect=AssertionError("the default session must not be built")):
            return asyncio.run(coroutine)
    finally:
        set_http_session_factory(None)


def test_component_base_http_session_factory_lookup(my_predbat):
    """
    The base's factory is found by a normal attribute lookup, so a class attribute and a property
    both work. A mock base answers every attribute, so an attribute it only invents is not taken for a factory.
    """
    print("\n*** Test: http_session factory lookup on the base ***")
    class_factory = FakeSessionFactory("class")
    property_factory = FakeSessionFactory("property")
    global_factory = FakeSessionFactory("global")

    class ClassAttributeBase(MockBase):
        """Base whose factory is a class attribute."""

        http_session_factory = class_factory

    class PropertyBase(MockBase):
        """Base whose factory is a property."""

        @property
        def http_session_factory(self):
            return property_factory

    assert TestComponent(ClassAttributeBase()).http_session().owner == "class", "a class attribute factory should be honoured"
    assert TestComponent(PropertyBase()).http_session().owner == "property", "a property factory should be honoured"

    set_http_session_factory(global_factory)
    try:
        mock_base = MagicMock()
        mock_base.local_tz = timezone.utc
        mock_base.prefix = "predbat"
        mock_base.args = {}
        assert TestComponent(mock_base).http_session().owner == "global", "a MagicMock base without a factory should fall back to the global factory"
    finally:
        set_http_session_factory(None)
    print("PASS: class attribute and property factories honoured, mock base's invented attribute ignored")
    return False


def test_component_base_http_session_octopus_wiring(my_predbat):
    """OctopusAPI hands its client the component's http_session, so the client's session comes from the base factory."""
    print("\n*** Test: Octopus client session comes from the base factory ***")
    factory = FakeSessionFactory("octopus")
    trap = FakeSessionFactory("global")
    component = OctopusAPI(FactoryBase(factory), key="key", account_id="A-1", automatic=False)
    session = _run_with_global_trap(trap, component.api.async_create_client_session())
    sessions = _assert_only_base_factory("octopus", factory, trap)
    assert session is sessions[0], "the client should hold the base factory's session"
    assert "skip_auto_headers" in session.kwargs, "the client's headers should be passed through the factory"
    print("PASS: octopus client session came from the base factory")
    return False


def test_component_base_http_session_ohme_wiring(my_predbat):
    """OhmeAPI's client builds its sessions from the base factory on both the token refresh and the request path."""
    print("\n*** Test: Ohme refresh and request sessions come from the base factory ***")
    # Token refresh path: an old token makes _async_refresh_session post to the token endpoint
    factory = FakeSessionFactory("ohme-refresh")
    trap = FakeSessionFactory("global")
    component = OhmeAPI(FactoryBase(factory), email="driver@example.com", password="secret")
    client = component.client
    client._token = "old"
    client._refresh_token = "refresh"
    client._token_birth = time.time() - 3000
    assert _run_with_global_trap(trap, client._async_refresh_session()) is True, "the token refresh should succeed through the fake session"
    sessions = _assert_only_base_factory("ohme refresh", factory, trap)
    assert any("securetoken" in url for _, url in sessions[0].requests), "the refresh should have posted to the token endpoint"

    # Request path: a fresh token skips the refresh, so _make_request builds the session itself
    factory = FakeSessionFactory("ohme-request")
    trap = FakeSessionFactory("global")
    component = OhmeAPI(FactoryBase(factory), email="driver@example.com", password="secret")
    client = component.client
    client._token = "token"
    client._refresh_token = "refresh"
    client._token_birth = time.time()
    result = _run_with_global_trap(trap, client._make_request("GET", "/v1/test"))
    assert result.get("ok") == 1, "the request should be answered by the fake session"
    sessions = _assert_only_base_factory("ohme request", factory, trap)
    assert any("api.ohme.io/v1/test" in url for _, url in sessions[0].requests), "the request should have been made on the base factory's session"
    print("PASS: ohme refresh and request sessions came from the base factory")
    return False


def test_component_base_http_session_hypervolt_socket(my_predbat):
    """The Hypervolt websocket reader opens its connection on a session from the base factory."""
    print("\n*** Test: Hypervolt websocket session comes from the base factory ***")
    factory = FakeSessionFactory("hypervolt")
    trap = FakeSessionFactory("global")
    component = HypervoltAPI(FactoryBase(factory), username="driver", password="secret")
    # A V3 charger's session reader returns after its one connection, which the fake closes at once
    link = ChargerLink("12345", 3)
    _run_with_global_trap(trap, component.transport._reader(link, "session"))
    sessions = _assert_only_base_factory("hypervolt socket", factory, trap)
    assert len(sessions[0].sockets) == 1 and sessions[0].sockets[0].endswith("12345/session/in-progress"), "the socket should have been opened on the base factory's session: {}".format(sessions[0].sockets)
    print("PASS: hypervolt websocket session came from the base factory")
    return False


def test_component_base_http_session_myenergi_direct(my_predbat):
    """The myenergi direct (digest) transport builds its session from the base factory, passing the digest middleware."""
    print("\n*** Test: myenergi direct transport session comes from the base factory ***")
    factory = FakeSessionFactory("myenergi")
    trap = FakeSessionFactory("global")
    component = MyEnergiAPI(FactoryBase(factory), auth_method="direct", hub_serial="12345678", api_key="secret-key")
    assert type(component.transport).__name__ == "MyEnergiDirectTransport", "the direct transport should have been built"

    async def make_session():
        return component.transport._new_session()

    session = _run_with_global_trap(trap, make_session())
    _assert_only_base_factory("myenergi direct", factory, trap)
    assert session.kwargs.get("middlewares") and "User-Agent" in session.kwargs.get("headers", {}), "the digest middleware and headers should reach the factory"
    print("PASS: myenergi direct session came from the base factory")
    return False


def test_component_base_http_session_kraken_mixin(my_predbat):
    """KrakenAuthMixin mints tokens on a session from its host's base factory."""
    print("\n*** Test: Kraken token request session comes from the base factory ***")
    from kraken_auth_mixin import KrakenAuthMixin

    class KrakenHost(KrakenAuthMixin, ComponentBase):
        """Minimal component carrying the mixin."""

        def initialize(self, **kwargs):
            self.base_url = "https://kraken.example"
            self._init_kraken_auth("api_key", key="sk_live_test")

        async def run(self, seconds, first):
            return True

    factory = FakeSessionFactory("kraken")
    trap = FakeSessionFactory("global")
    host = KrakenHost(FactoryBase(factory))
    result = _run_with_global_trap(trap, host._kraken_token_request({"APIKey": "sk_live_test"}))
    sessions = _assert_only_base_factory("kraken mixin", factory, trap)
    assert any("kraken.example/v1/graphql" in url for _, url in sessions[0].requests), "the mutation should have been posted on the base factory's session"
    assert result is None, "the fake reply carries no token, so the mint reports failure"
    print("PASS: kraken token request session came from the base factory")
    return False


class FakeClock:
    """base.component_clock stand-in with fixed readings and a recording, non-waiting sleep."""

    def __init__(self, mono=12.5, wall=1234.5):
        """Start with the given readings and no recorded sleeps."""
        self.mono = mono
        self.wall = wall
        self.sleeps = []

    def monotonic(self):
        """Fixed monotonic reading."""
        return self.mono

    def time(self):
        """Fixed wall-clock reading."""
        return self.wall

    async def sleep(self, seconds):
        """Record the request and return at once."""
        self.sleeps.append(seconds)


def _mock_base():
    """A MagicMock base, which answers every attribute lookup."""
    mock_base = MagicMock()
    mock_base.local_tz = timezone.utc
    mock_base.prefix = "predbat"
    mock_base.args = {}
    return mock_base


def test_component_base_mqtt_client(my_predbat):
    """
    mqtt_client prefers base.mqtt_client_factory, passing the keyword arguments straight through, and
    otherwise builds aiomqtt.Client looked up at call time. An attribute a mock base only invents is never taken for a factory,
    and a missing aiomqtt raises a clear ImportError.
    """
    print("\n*** Test: mqtt_client lookup order and default ***")
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return "from-base-factory"

    base = MockBase()
    component = TestComponent(base)

    with patch("aiomqtt.Client", return_value="from-aiomqtt") as default_client:
        assert component.mqtt_client(hostname="h", port=1) == "from-aiomqtt", "without a factory the default aiomqtt.Client should be used"
        default_client.assert_called_once_with(hostname="h", port=1)

        base.mqtt_client_factory = factory
        assert component.mqtt_client(hostname="h", port=2, username="u") == "from-base-factory", "the base factory should be used when set"
        assert calls == [{"hostname": "h", "port": 2, "username": "u"}], "the factory should receive the keyword arguments unchanged, got {}".format(calls)
        assert default_client.call_count == 1, "aiomqtt.Client must not be built when the base has a factory"

        del base.mqtt_client_factory
        assert component.mqtt_client(hostname="h", port=3) == "from-aiomqtt", "removing the factory should restore the default"

        assert TestComponent(_mock_base()).mqtt_client(hostname="m") == "from-aiomqtt", "a MagicMock base without a factory should fall back to aiomqtt.Client"

    with patch.dict(sys.modules, {"aiomqtt": None}):
        try:
            component.mqtt_client(hostname="h")
            raised = False
        except ImportError as exc:
            raised = "aiomqtt" in str(exc)
        assert raised, "a missing aiomqtt and no factory should raise an ImportError naming aiomqtt"
        base.mqtt_client_factory = factory
        assert component.mqtt_client(hostname="h") == "from-base-factory", "a factory needs no aiomqtt"
    print("PASS: base factory, default, mock base and missing aiomqtt handled")
    return False


def test_component_base_clock(my_predbat):
    """
    monotonic, wall_time and sleep use base.component_clock when set and otherwise time.monotonic, time.time and
    asyncio.sleep looked up at call time. An attribute a mock base only invents is never taken for a clock.
    """
    print("\n*** Test: clock methods lookup order and default ***")
    base = MockBase()
    component = TestComponent(base)

    with patch("time.monotonic", return_value=111.0), patch("time.time", return_value=222.0):
        assert component.monotonic() == 111.0, "monotonic should default to time.monotonic"
        assert component.wall_time() == 222.0, "wall_time should default to time.time"
    with patch("asyncio.sleep", new_callable=AsyncMock) as real_sleep:
        asyncio.run(component.sleep(7))
        real_sleep.assert_awaited_once_with(7)

    clock = FakeClock()
    base.component_clock = clock
    with patch("time.monotonic", side_effect=AssertionError("time.monotonic must not be used")), patch("time.time", side_effect=AssertionError("time.time must not be used")):
        assert component.monotonic() == 12.5, "monotonic should come from base.component_clock"
        assert component.wall_time() == 1234.5, "wall_time should come from base.component_clock"
    with patch("asyncio.sleep", side_effect=AssertionError("asyncio.sleep must not be used")):
        asyncio.run(component.sleep(3))
        asyncio.run(component.sleep(0.5))
    assert clock.sleeps == [3, 0.5], "sleep should go to base.component_clock, got {}".format(clock.sleeps)

    del base.component_clock
    with patch("time.monotonic", return_value=5.0):
        assert component.monotonic() == 5.0, "removing the clock should restore the default"

    mock_component = TestComponent(_mock_base())
    with patch("time.monotonic", return_value=9.0), patch("time.time", return_value=8.0):
        assert mock_component.monotonic() == 9.0 and mock_component.wall_time() == 8.0, "a MagicMock base without a clock should fall back to the time module"
    with patch("asyncio.sleep", new_callable=AsyncMock) as real_sleep:
        asyncio.run(mock_component.sleep(2))
        real_sleep.assert_awaited_once_with(2)
    print("PASS: base clock, default and mock base handled")
    return False


def test_component_base_clock_isolated_per_instance(my_predbat):
    """Two components on separate bases with separate clocks only see their own clock."""
    print("\n*** Test: clocks are per instance ***")
    base_a, base_b = MockBase(), MockBase()
    base_a.component_clock = FakeClock(mono=1.0, wall=100.0)
    base_b.component_clock = FakeClock(mono=2.0, wall=200.0)
    comp_a, comp_b = TestComponent(base_a), TestComponent(base_b)

    async def both():
        await asyncio.gather(comp_a.sleep(10), comp_b.sleep(20))

    asyncio.run(both())
    assert (comp_a.monotonic(), comp_a.wall_time()) == (1.0, 100.0), "instance A should read its own clock"
    assert (comp_b.monotonic(), comp_b.wall_time()) == (2.0, 200.0), "instance B should read its own clock"
    assert base_a.component_clock.sleeps == [10] and base_b.component_clock.sleeps == [20], "each clock should only see its own instance's sleeps"
    print("PASS: clocks are isolated per instance")
    return False


def test_component_base_ignores_unrelated_clock_attribute(my_predbat):
    """A host base whose own `clock` attribute is a callable (not a component clock) must not be mistaken for one."""
    print("\n*** Test: an unrelated base.clock is ignored ***")
    base = MockBase()
    base.clock = lambda: "2026-01-01 00:00:00"
    component = TestComponent(base)
    with patch("time.monotonic", return_value=31.0), patch("time.time", return_value=32.0):
        assert component.monotonic() == 31.0, "monotonic should use real time, not base.clock"
        assert component.wall_time() == 32.0, "wall_time should use real time, not base.clock"
    with patch("asyncio.sleep", new_callable=AsyncMock) as real_sleep:
        asyncio.run(component.sleep(6))
        real_sleep.assert_awaited_once_with(6)
    print("PASS: unrelated base.clock ignored")
    return False


def test_component_base_override_on_mock_base(my_predbat):
    """
    On a mock base only an explicitly set override counts: a MagicMock set on purpose as a factory or
    clock is used, one the mock made up is ignored, and a class attribute or property on a real base
    is still honoured, for http_session, mqtt_client and the clock alike.
    """
    print("\n*** Test: overrides on a mock base ***")
    http_factory = MagicMock(return_value="http-from-mock")
    mqtt_factory = MagicMock(return_value="mqtt-from-mock")
    clock = MagicMock()
    clock.monotonic.return_value = 41.0
    clock.time.return_value = 42.0
    clock.sleep = AsyncMock()

    explicit = _mock_base()
    explicit.http_session_factory = http_factory
    explicit.mqtt_client_factory = mqtt_factory
    explicit.component_clock = clock
    component = TestComponent(explicit)
    assert component.http_session(timeout=1) == "http-from-mock", "an explicitly set MagicMock http factory should be used"
    http_factory.assert_called_once_with(timeout=1)
    assert component.mqtt_client(hostname="h") == "mqtt-from-mock", "an explicitly set MagicMock mqtt factory should be used"
    mqtt_factory.assert_called_once_with(hostname="h")
    assert component.monotonic() == 41.0 and component.wall_time() == 42.0, "an explicitly set MagicMock clock should be used"
    asyncio.run(component.sleep(4))
    clock.sleep.assert_awaited_once_with(4)

    unset = TestComponent(_mock_base())
    with patch("aiomqtt.Client", return_value="default-mqtt"), patch("time.monotonic", return_value=1.5), patch("aiohttp.ClientSession", return_value="default-http"):
        assert unset.http_session() == "default-http", "an unset attribute on a mock base should be ignored for http_session"
        assert unset.mqtt_client() == "default-mqtt", "an unset attribute on a mock base should be ignored for mqtt_client"
        assert unset.monotonic() == 1.5, "an unset attribute on a mock base should be ignored for the clock"

    class RealBase(MockBase):
        """Real base with class attribute and property overrides."""

        mqtt_client_factory = staticmethod(lambda **kwargs: "class-mqtt")

        @property
        def component_clock(self):
            return FakeClock(mono=7.0)

    real = TestComponent(RealBase())
    assert real.mqtt_client() == "class-mqtt" and real.monotonic() == 7.0, "class attribute and property overrides on a real base should be honoured"
    print("PASS: explicit mock overrides used, made-up ones ignored, real base lookups honoured")
    return False


def test_component_base_import_skips_unittest_mock(my_predbat):
    """
    Importing Predbat must not load unittest.mock: it costs about 6 MB of idle memory on every
    install, and _base_override() only needs it when a test has already imported it.
    """
    print("\n*** Test: importing Predbat does not load unittest.mock ***")
    import os
    import subprocess

    import component_base

    source_dir = os.path.dirname(os.path.abspath(component_base.__file__))
    code = "import sys; sys.path.insert(0, {!r}); import predbat; print('MOCK_LOADED' if 'unittest.mock' in sys.modules else 'MOCK_ABSENT')".format(source_dir)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=source_dir)
    if "MOCK_ABSENT" not in result.stdout:
        print("ERROR: unittest.mock was loaded by importing predbat (rc {}): {}".format(result.returncode, (result.stdout + result.stderr)[-800:]))
        return True
    print("PASS: predbat imports without unittest.mock")
    return False


def test_component_base_write_methods(my_predbat):
    """
    @writes only marks a method: it returns the same function, which behaves exactly as before.
    write_methods() lists exactly the marked methods, inherited ones included, and an unmarked
    override unmarks a method.
    """
    print("\n*** Test: @writes marks methods and write_methods lists exactly those ***")

    def plain(self):
        return "result"

    assert writes(plain) is plain, "@writes should return the same function"
    assert plain(None) == "result", "a marked method should behave exactly as before"

    class Writer(TestComponent):
        @writes
        async def set_mode(self, mode):
            return mode

        @writes
        def set_reserve(self, value):
            return value

        def read_status(self):
            return "status"

    class Child(Writer):
        def set_reserve(self, value):
            """Overridden without @writes, so no longer listed."""
            return value

        @writes
        def set_rate(self, value):
            return value

    assert TestComponent.write_methods() == [], "a component with no marked methods lists none, got {}".format(TestComponent.write_methods())
    assert Writer.write_methods() == ["set_mode", "set_reserve"], "got {}".format(Writer.write_methods())
    assert Child.write_methods() == ["set_mode", "set_rate"], "got {}".format(Child.write_methods())
    writer = Writer(MockBase())
    assert writer.write_methods() == ["set_mode", "set_reserve"], "write_methods should work on an instance too"
    assert asyncio.run(writer.set_mode("eco")) == "eco", "a marked coroutine should still be awaitable and return its value"
    print("PASS: @writes marks, write_methods lists exactly the marked methods")
    return False


def test_component_base_write_methods_components(my_predbat):
    """
    Spot check real components: a method that sends a control write is listed, a known read is not.
    """
    print("\n*** Test: component writer methods are listed and readers are not ***")
    cases = [
        (FoxAPI, ["set_device_setting", "set_scheduler", "set_battery_charging_time"], ["get_device_setting", "get_scheduler", "get_real_time_data", "run"]),
        (SolisAPI, ["write_cid", "read_and_write_cid", "write_time_windows_if_changed"], ["read_cid", "read_batch", "get_inverter_detail", "run"]),
        (TeslemetryAPI, ["set_operation_mode", "set_backup_reserve", "set_tariff"], ["run", "publish_site_info"]),
        (OhmeAPI, ["charger_control_send", "charger_control_release_one", "charger_control_hand_to_octopus", "select_event_handler", "number_event_handler", "switch_event_handler"], ["run", "publish_data", "select_event", "apply_slot_mode"]),
        (MyEnergiAPI, ["charger_control_send", "charger_control_release_one", "charger_control_hand_to_octopus", "switch_event_handler"], ["run", "publish_data", "switch_event", "number_event_handler"]),
        (HypervoltAPI, ["send_command", "charger_control_send", "charger_control_release_one", "charger_control_hand_to_octopus", "switch_event_handler", "number_event_handler", "select_event_handler"], ["run", "publish_data", "switch_event"]),
        (WallboxAPI, ["charger_control_send", "charger_control_release_one", "charger_control_hand_to_octopus", "switch_event_handler", "number_event_handler", "select_event_handler"], ["run", "publish_data", "switch_event", "poll"]),
    ]
    for cls, writers, readers in cases:
        listed = cls.write_methods()
        for name in writers:
            assert name in listed, "{}.{} sends a write and should be listed".format(cls.__name__, name)
        for name in readers:
            assert hasattr(cls, name), "{}.{} should exist for this check to mean anything".format(cls.__name__, name)
            assert name not in listed, "{}.{} only reads and should not be listed".format(cls.__name__, name)
    print("PASS: writer methods listed, readers not")
    return False


# Constructors that must only be called through ComponentBase.http_session() / mqtt_client(), so a
# host or a test can inject the client for one component instance.
CLIENT_CONSTRUCTORS = {("aiohttp", "ClientSession"): "self.http_session()", ("aiomqtt", "Client"): "self.mqtt_client()"}

# Modules that may construct the clients directly, and why.
CONSTRUCTION_ALLOWED = {
    "component_base": "it holds the default factories the components fall back to",
    # The web page imports the annual tool's config and validation, but runs the tool itself as a
    # separate annual_cli.py process, so these fetches never run inside a component.
    "annual_http": "annual tool fetch helper, runs in the annual_cli.py process",
    "annual_load": "annual tool fetch helper, runs in the annual_cli.py process",
    "http_sync": "the synchronous HTTP helper: it uses new_http_session() once component_base is loaded, and builds its own session only before that (download.py repairing an install)",
}


def component_module_closure(source_dir):
    """Module names of every registered component plus every apps/predbat module they import, transitively."""
    import ast
    import os

    from components import COMPONENT_LIST

    def local_imports(name):
        with open(os.path.join(source_dir, name + ".py")) as handle:
            tree = ast.parse(handle.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
        return {module for module in found if os.path.isfile(os.path.join(source_dir, module + ".py"))}

    pending = [entry["class"].split(".")[0] for entry in COMPONENT_LIST.values()]
    seen = set()
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(local_imports(name) - seen)
    return seen


def direct_client_constructions(source):
    """(line, constructor) for every direct aiohttp.ClientSession / aiomqtt.Client call in the source, however imported."""
    import ast

    tree = ast.parse(source)
    module_alias = {}
    name_alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("aiohttp", "aiomqtt"):
                    module_alias[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("aiohttp", "aiomqtt"):
            for alias in node.names:
                if (node.module, alias.name) in CLIENT_CONSTRUCTORS:
                    name_alias[alias.asname or alias.name] = (node.module, alias.name)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in module_alias:
            target = (module_alias[func.value.id], func.attr)
        elif isinstance(func, ast.Name) and func.id in name_alias:
            target = name_alias[func.id]
        if target in CLIENT_CONSTRUCTORS:
            found.append((node.lineno, "{}.{}".format(*target)))
    return found


def test_component_base_no_direct_client_construction(my_predbat):
    """Components and the clients they own build HTTP sessions and MQTT clients only through the ComponentBase factories"""
    import os

    import component_base

    source_dir = os.path.dirname(os.path.abspath(component_base.__file__))
    modules = component_module_closure(source_dir)
    assert len(modules) > 20, "the component closure looks wrong: {}".format(sorted(modules))
    for name in ("fox", "sigenergy", "gateway", "octopus", "ohme"):
        assert name in modules, "{} should be in the component closure".format(name)

    offenders = []
    for name in sorted(modules - set(CONSTRUCTION_ALLOWED)):
        with open(os.path.join(source_dir, name + ".py")) as handle:
            for line, constructor in direct_client_constructions(handle.read()):
                offenders.append("{}.py:{} calls {} directly".format(name, line, constructor))
    assert not offenders, "use {} instead:\n{}".format(" / ".join(CLIENT_CONSTRUCTORS.values()), "\n".join(offenders))

    # The scanner itself must see every import style, or a clean result means nothing.
    probe = "import aiohttp\nimport aiomqtt as mq\nfrom aiohttp import ClientSession as CS\nfrom aiomqtt import Client\n" "aiohttp.ClientSession()\nmq.Client(hostname='h')\nCS()\nClient(hostname='h')\naiohttp.ClientTimeout(total=1)\n"
    assert [line for line, _ in direct_client_constructions(probe)] == [5, 6, 7, 8], direct_client_constructions(probe)
    print("PASS: {} component modules build clients only through the factories".format(len(modules)))
    return False


class _FakeClock:
    """One clock behind time.time(), time.monotonic() and asyncio.sleep(), so a test can see which wall-clock second each run() lands on."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    async def sleep(self, delay, result=None):
        """Advance the clock by the whole delay at once, then yield to the event loop."""
        self.now += max(delay, 0)
        await _original_sleep(0)
        return result


def test_component_base_align_minute(my_predbat):
    """A component whose registry entry sets align_minute runs just after each wall-clock minute boundary.

    The default loop counts 5 second sleeps from process start, so its runs fall wherever the pod's start
    put them and slip by each run()'s own duration. A component acting on minute-resolution windows (the
    Teslemetry signal tariff) wants its run in the first second or so of the minute instead. A slow run()
    must not drift the later runs off the boundary, and run()'s seconds must step in whole minutes so the
    interval checks inside run() (e.g. a 120 second poll) keep their period.
    """
    print("\n*** Test: ComponentBase align_minute runs on the minute boundary ***")
    base_epoch = 1800000000  # a whole minute
    clock = _FakeClock(base_epoch + 37.5)
    runs = []

    class AlignedComponent(ComponentBase):
        def initialize(self, **kwargs):
            pass

        async def run(self, seconds, first):
            runs.append((clock.now - base_epoch, seconds, first))
            # Each run costs 7 seconds of API calls, except the third, which overruns the next boundary
            clock.now += 70 if len(runs) == 3 else 7
            return True

    async def run_test():
        with patch("asyncio.sleep", side_effect=clock.sleep), patch("component_base.time", SimpleNamespace(time=clock.time, monotonic=clock.monotonic)):
            component = AlignedComponent(MockBase())
            component.align_minute = True
            task = asyncio.create_task(component.start())
            for _ in range(10000):
                if len(runs) >= 5:
                    break
                await _original_sleep(0)
            await component.stop()
            await task

    asyncio.run(run_test())

    assert len(runs) >= 5, "Expected at least 5 runs, got {}".format(runs)
    times = [run[0] for run in runs[:5]]
    seconds = [run[1] for run in runs[:5]]
    firsts = [run[2] for run in runs[:5]]
    assert firsts == [True, False, False, False, False], firsts
    # The startup run is immediate; every later one is 1 second past a boundary, and the 70 second
    # third run skips the boundary it overran rather than running late at 191.
    assert times == [37.5, 61, 121, 241, 301], times
    assert seconds == [0, 60, 120, 240, 300], seconds
    print("PASS: aligned runs land at :01 and seconds step in whole minutes")
    return False


def test_component_base_align_minute_defaults_off(my_predbat):
    """A component built without the registry (the standalone CLI harnesses) keeps the default free-running loop."""
    component = TestComponent(MockBase())
    assert component.align_minute is False, component.align_minute
    return False


def test_component_base_all(my_predbat):
    """Run all component_base tests"""
    tests = [
        ("not_calculating", test_component_base_not_calculating, "Component defaults to not calculating"),
        ("immediate_success", test_component_base_immediate_success, "Component starts immediately on first successful run"),
        ("backoff_sequence", test_component_base_backoff_sequence, "Component uses backoff on startup failures"),
        ("stop_during_backoff", test_component_base_stop_during_backoff, "Component respects api_stop during backoff"),
        ("normal_operation", test_component_base_normal_operation_after_start, "Component runs every 60s after start"),
        ("exception_handling", test_component_base_exception_handling, "Component handles exceptions with backoff"),
        ("run_timeout", test_component_base_run_timeout, "Hung run() triggers timeout, stack trace, and error count"),
        ("align_minute", test_component_base_align_minute, "align_minute runs just after each minute boundary without drift"),
        ("align_minute_default_off", test_component_base_align_minute_defaults_off, "align_minute is off unless the registry sets it"),
        ("first_cleared_preset", test_component_base_first_cleared_when_run_presets_api_started, "first flag clears even when run() pre-sets api_started"),
        ("set_arg_auto", test_component_base_set_arg_auto, "set_arg_auto warns once on an apps.yaml override, silent otherwise"),
        ("set_arg_auto_keep", test_component_base_set_arg_auto_keeps_user_setting, "set_arg_auto(overwrite=False) keeps an explicit apps.yaml setting"),
        ("set_state_external", test_component_base_set_state_external, "set_state_external forwards to the HA interface"),
        ("midnight_utc_rewound", test_component_base_midnight_utc_ignores_rewound_base, "midnight_utc ignores a rewound base.midnight_utc"),
        ("minutes_now_derived", test_component_base_minutes_now_follows_update_time, "minutes_now follows update_time and ignores the faked value"),
        ("minutes_now_snapshot", test_component_base_minutes_now_snapshots_the_clock, "minutes_now snapshots now_utc rather than reading it twice"),
        ("discovery_refresh_churn", test_component_base_refresh_discovery_files_and_stops_churning, "refresh_discovery files a changed report, not an unchanged one"),
        ("discovery_refresh_none", test_component_base_refresh_discovery_skips_none_and_missing_build, "refresh_discovery skips a None report and a non-reporting component"),
        ("discovery_refresh_failure", test_component_base_refresh_discovery_failure_is_contained_and_retried, "a build_discovery failure is contained and retried"),
        ("discovery_refresh_marker_order", test_component_base_refresh_discovery_marker_waits_for_the_report_to_land, "the marker advances only after the report is filed"),
        ("discovery_refresh_no_init", test_component_base_refresh_discovery_survives_a_component_built_without_init, "refresh_discovery does not raise on a component built without __init__"),
        ("discovery_refresh_live_state", test_component_base_refresh_discovery_refiles_when_live_state_behind_inverter_record_grows, "a report built from live state is re-filed when that state grows"),
        ("discovery_entities_filter", test_component_base_discovery_entities_keeps_only_what_exists, "discovery_entities keeps only entities that exist"),
        ("request_replan", test_component_base_request_replan, "request_replan sets update_pending without invalidating the plan"),
        ("on_component_loop", test_component_base_on_component_loop, "on_component_loop runs work on the component's own event loop from any caller"),
        ("http_session_default", test_component_base_http_session_default, "http_session defaults to a new caller-owned aiohttp.ClientSession"),
        ("http_session_factory", test_component_base_http_session_factory, "an installed HTTP session factory is used, None restores the default"),
        ("http_session_order", test_component_base_http_session_resolution_order, "http_session prefers the base factory, then the global factory, then aiohttp"),
        ("http_session_isolated", test_component_base_http_session_isolated_per_instance, "two instances' components only get sessions from their own factory"),
        ("http_session_close", test_component_base_http_session_close_isolation, "closing one instance's session leaves another's open; an exception still closes"),
        ("http_session_lookup", test_component_base_http_session_factory_lookup, "a class attribute or property factory is honoured, a mock base is not taken for one"),
        ("http_session_octopus", test_component_base_http_session_octopus_wiring, "the Octopus client's session comes from the base factory"),
        ("http_session_ohme", test_component_base_http_session_ohme_wiring, "Ohme refresh and request sessions come from the base factory"),
        ("http_session_hypervolt", test_component_base_http_session_hypervolt_socket, "the Hypervolt websocket session comes from the base factory"),
        ("http_session_myenergi", test_component_base_http_session_myenergi_direct, "the myenergi direct transport session comes from the base factory"),
        ("http_session_kraken", test_component_base_http_session_kraken_mixin, "the Kraken token request session comes from the base factory"),
        ("mqtt_client", test_component_base_mqtt_client, "mqtt_client prefers the base factory, then aiomqtt.Client, an attribute a mock base only invents is ignored, one set explicitly is used"),
        ("clock", test_component_base_clock, "monotonic, wall_time and sleep prefer base.component_clock, then the time module and asyncio, an attribute a mock base only invents is ignored, one set explicitly is used"),
        ("clock_unrelated", test_component_base_ignores_unrelated_clock_attribute, "a base with its own unrelated clock attribute is ignored and real time is used"),
        ("clock_isolated", test_component_base_clock_isolated_per_instance, "two instances' clocks are separate"),
        ("override_on_mock_base", test_component_base_override_on_mock_base, "an explicitly set override on a mock base is used, a made-up one is ignored"),
        ("import_skips_unittest_mock", test_component_base_import_skips_unittest_mock, "importing predbat does not load unittest.mock"),
        ("write_methods", test_component_base_write_methods, "@writes marks a method and write_methods lists exactly the marked ones"),
        ("write_methods_components", test_component_base_write_methods_components, "component writer methods are listed and readers are not"),
        ("no_direct_clients", test_component_base_no_direct_client_construction, "components and their clients build HTTP sessions and MQTT clients only through the factories"),
    ]

    failed = []
    for name, test_func, description in tests:
        print(f"\n*** Running: {name} - {description} ***")
        try:
            result = test_func(my_predbat)
            if result:
                failed.append(name)
                print(f"FAILED: {name}")
        except Exception as e:
            failed.append(name)
            print(f"ERROR in {name}: {e}")

    if failed:
        print(f"\n*** {len(failed)} test(s) failed: {', '.join(failed)} ***")
        return True  # True = test failed
    else:
        print(f"\n*** All {len(tests)} component_base tests passed ***")
        return False  # False = test passed
