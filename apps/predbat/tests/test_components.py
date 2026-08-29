# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on

"""Tests for Components.initialize()'s "should we warn about a skipped component" heuristic."""

from components import Components
from mock_base import MockBase


def _skip_warnings(base):
    """Return the "Warn: Skipping ..." log lines recorded on a MockBase-backed run."""
    return [message for message in base.log_messages if message.startswith("Warn: Skipping")]


class LoggingMockBase(MockBase):
    """MockBase that also records every log message, like the other component test mocks."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.log_messages = []

    def log(self, message, quiet=True):
        """Record a log message instead of printing it."""
        self.log_messages.append(message)


def test_gecloud_data_no_warning_from_global_days_previous(my_predbat):
    """days_previous is a global load-forecasting setting nearly every installation sets.

    Its presence alone must not be treated as evidence the user tried to enable GE Cloud
    Data, or everyone who has never touched GE Cloud settings gets a spurious warning.
    """
    base = LoggingMockBase(days_previous=[7])
    components = Components(base)
    components.initialize(only="gecloud_data", phase=1)

    assert _skip_warnings(base) == [], f"Unexpected warning(s) for a base that never configured GE Cloud Data: {_skip_warnings(base)}"
    return False


def test_gecloud_data_warns_when_actually_misconfigured(my_predbat):
    """When the user genuinely starts configuring GE Cloud Data but leaves it incomplete, warn."""
    base = LoggingMockBase(days_previous=[7], ge_cloud_data=True)
    components = Components(base)
    components.initialize(only="gecloud_data", phase=1)

    warnings = _skip_warnings(base)
    assert len(warnings) == 1, f"Expected exactly one warning, got: {warnings}"
    assert "GivEnergy Cloud Data" in warnings[0]
    assert "ge_cloud_key" in warnings[0]
    return False


def test_components_all(my_predbat):
    """Run all components.py tests"""
    tests = [
        ("gecloud_data_no_warning_from_global_days_previous", test_gecloud_data_no_warning_from_global_days_previous, "days_previous alone must not trigger a GE Cloud Data warning"),
        ("gecloud_data_warns_when_actually_misconfigured", test_gecloud_data_warns_when_actually_misconfigured, "GE Cloud Data still warns once genuinely (partially) configured"),
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
        print(f"\n*** All {len(tests)} components tests passed ***")
        return False  # False = test passed
