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
Tests for ComponentBase start method and backoff behavior
"""

import asyncio
from datetime import timezone
from unittest.mock import patch

from component_base import ComponentBase


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


def test_component_base_immediate_success(my_predbat):
    """Test component that succeeds on first run"""
    print("\n*** Test: ComponentBase immediate success ***")

    async def run_test():
        with patch("asyncio.sleep", side_effect=fast_sleep):
            base = MockBase()
            component = TestComponent(base, fail_until_attempt=0, return_true_on_run=True)

            # Start component in background
            task = asyncio.create_task(component.start())

            # Wait briefly for it to start
            await asyncio.sleep(0.01)

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
            await asyncio.sleep(0.01)
            assert component.run_count == 1, f"Expected 1 run after start, got {component.run_count}"
            assert not component.api_started, "Component should not have started yet (failed first attempt)"

            # Wait slightly longer - should still be 1 run (waiting for backoff)
            await asyncio.sleep(0.05)
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
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
            assert component.api_started, "Component should have started"
            initial_run_count = component.run_count

            # Wait a bit more - should not run again immediately (only every 60 seconds)
            await asyncio.sleep(0.05)
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
            await asyncio.sleep(0.01)
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


def test_component_base_all(my_predbat):
    """Run all component_base tests"""
    tests = [
        ("immediate_success", test_component_base_immediate_success, "Component starts immediately on first successful run"),
        ("backoff_sequence", test_component_base_backoff_sequence, "Component uses backoff on startup failures"),
        ("stop_during_backoff", test_component_base_stop_during_backoff, "Component respects api_stop during backoff"),
        ("normal_operation", test_component_base_normal_operation_after_start, "Component runs every 60s after start"),
        ("exception_handling", test_component_base_exception_handling, "Component handles exceptions with backoff"),
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
