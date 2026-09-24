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
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from component_base import ComponentBase
from coordinator import inverter_record


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

            # Wait long enough (sped up 100x by fast_sleep → ~2s real) for the component
            # loop to advance past simulated seconds=60 so a steady-state run can occur.
            await asyncio.sleep(200)

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
