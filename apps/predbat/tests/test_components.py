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

"""Tests for Components.initialize(): the lazy component imports and the "should we warn about a skipped component" heuristic."""

import inspect
import os
import subprocess
import sys

import components
from components import COMPONENT_LIST, Components, load_component_class
from component_base import ComponentBase
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


def test_every_component_class_imports(my_predbat):
    """Every registry entry's module imports cleanly and names a ComponentBase subclass.

    Component modules are only imported when their component is enabled (load_component_class()),
    so starting Predbat no longer compiles all of them - a syntax error in solis.py, a broken
    import in fox.py or a mistyped "class" path would otherwise first be seen by the one user who
    enables that component. This is the quick-suite check that imports every one of them.
    """
    failed = []
    for name, info in COMPONENT_LIST.items():
        try:
            cls = load_component_class(info)
        except Exception as e:
            print(f"ERROR: component '{name}' ({info['class']}) failed to import: {type(e).__name__}: {e}")
            failed.append(name)
            continue
        module_name, _, class_name = info["class"].rpartition(".")
        if not inspect.isclass(cls) or cls.__name__ != class_name or cls.__module__ != module_name:
            print(f"ERROR: component '{name}' path {info['class']} resolved to {cls!r}")
            failed.append(name)
        elif not issubclass(cls, ComponentBase):
            print(f"ERROR: component '{name}' class {info['class']} does not inherit ComponentBase")
            failed.append(name)
    if not failed:
        print(f"  All {len(COMPONENT_LIST)} component classes import")
    return bool(failed)


def test_importing_predbat_leaves_components_unloaded(my_predbat):
    """A bare `import predbat` must not pull in the lazily-registered component modules.

    The memory saving from load_component_class() only holds while nothing else imports those
    modules at startup, so this runs the import in a fresh interpreter (this process loaded
    everything long ago) and lists which registry modules arrived with it. ha, octopus and axle
    are imported by the core (predbat.py, fetch.py) and are expected; numpy is the one that
    matters most, load_ml_component being the only route to it.
    """
    modules = sorted({info["class"].rpartition(".")[0] for info in COMPONENT_LIST.values()})
    script = "import sys, predbat; print('LOADED:' + ' '.join(sorted(m for m in sys.modules if m in {!r} or m == 'numpy')))".format(modules)
    result = subprocess.run([sys.executable, "-c", script], cwd=os.path.dirname(os.path.abspath(components.__file__)), capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"ERROR: import predbat failed in a fresh interpreter:\n{result.stderr[-2000:]}")
        return True
    loaded = set()
    for line in result.stdout.splitlines():
        if line.startswith("LOADED:"):
            loaded = set(line[len("LOADED:") :].split())
    expected = {"ha", "octopus", "axle"}
    unexpected = loaded - expected
    if unexpected:
        print(f"ERROR: importing predbat also imported {sorted(unexpected)}; something outside the registry imports them at startup (add to the expected set here only if that is intended)")
        return True
    print(f"  import predbat loaded only the expected component modules {sorted(loaded)}")
    return False


def _load_errors(base):
    """Return the "Error: Cannot initialise ..." log lines recorded on a MockBase-backed run."""
    return [message for message in base.log_messages if message.startswith("Error: Cannot initialise")]


def _with_import_module(replacement, run):
    """Run a callable with components.importlib.import_module swapped for a stand-in."""
    original = components.importlib.import_module
    components.importlib.import_module = replacement
    try:
        return run()
    finally:
        components.importlib.import_module = original


def test_import_failure_is_an_error_not_disabled(my_predbat):
    """A component whose module will not import is left inactive and reported as an error, without stopping Predbat.

    Forces importlib.import_module to raise ImportError (what gateway.py does without protobuf)
    and initialises the storage component through the registry. That used to be caught at
    import time by leaving the entry out of COMPONENT_LIST, which made a configured component
    silently vanish; now the failure is recorded so the status page can show it.
    """
    base = LoggingMockBase()
    comps = Components(base)

    def _missing(_name):
        """Stand-in for importlib.import_module on an install without the package."""
        raise ImportError("No module named 'google.protobuf'")

    _with_import_module(_missing, lambda: comps.initialize(only="storage", phase=0))

    assert comps.components.get("storage") is None, "the component must be left uninitialised"
    assert not comps.is_active("storage"), "a component that never loaded is not active"
    assert comps.is_alive("storage"), "and must not count as a dead process for is_all_alive()"
    assert "google.protobuf" in (comps.load_error("storage") or ""), comps.load_error("storage")
    assert comps.load_error("ha") is None, "only the failed component carries an error"
    errors = _load_errors(base)
    assert len(errors) == 1, f"Expected exactly one error, got: {errors}"
    assert "Storage interface" in errors[0] and "google.protobuf" in errors[0], errors[0]
    assert not _skip_warnings(base), "a load failure is an error, not a configuration skip"
    assert not any("Traceback" in message for message in base.log_messages), "a missing package needs no traceback"

    # A re-initialise that succeeds clears the error, as a restart from the status page would
    comps.initialize(only="storage", phase=0)
    assert comps.components.get("storage") is not None, "the real storage component constructs"
    assert comps.load_error("storage") is None, "the error is cleared once the component loads"
    return False


def test_broken_component_does_not_take_the_others_down(my_predbat):
    """An exception from one component's constructor is contained: it is recorded and the rest of the phase still initialises."""

    class _Stub(ComponentBase):
        """A component that constructs."""

        def initialize(self, **kwargs):
            """Nothing to set up."""

    class _Broken(ComponentBase):
        """A component whose constructor blows up."""

        def initialize(self, **kwargs):
            """Fail the way a bug in a component's own initialise() would."""
            raise RuntimeError("boom")

    class _FakeModule:
        """Answers any class name with the stub, except the one under test."""

        def __getattr__(self, name):
            return _Broken if name == "StorageComponent" else _Stub

    base = LoggingMockBase()
    comps = Components(base)
    _with_import_module(lambda _name: _FakeModule(), lambda: comps.initialize(phase=0))

    assert comps.components.get("storage") is None, "the broken component is left uninitialised"
    assert comps.load_error("storage") == "boom", comps.load_error("storage")
    for name in ("ha", "ha_history", "web"):
        assert isinstance(comps.components.get(name), _Stub), f"{name} must still initialise after storage failed"
        assert comps.load_error(name) is None
    errors = _load_errors(base)
    assert len(errors) == 1 and "Storage interface" in errors[0] and "boom" in errors[0], errors
    assert any("Traceback" in message and "boom" in message for message in base.log_messages), "a crash in a component's own code is logged with its traceback"
    return False


def test_load_error_is_reported_as_a_component_error(my_predbat):
    """record_final_run_status() reports a component that failed to load as an error, not as disabled.

    Runs the real reporting against a Components instance where one entry carries a load error,
    and checks the health sensor and the recorded run status, since that is what a user sees.
    """
    original_components = my_predbat.components
    original_had_errors = my_predbat.had_errors
    base = LoggingMockBase()
    comps = Components(base)
    comps.initialize(only="storage", phase=0)
    comps.components["storage"] = None
    comps.component_errors["storage"] = "No module named 'google.protobuf'"
    my_predbat.components = comps
    my_predbat.had_errors = False
    try:
        my_predbat.record_final_run_status("Idle", "")
        healthy = my_predbat.dashboard_values.get("binary_sensor.predbat_components_healthy", {})
        assert healthy.get("state") == "off", healthy
        attributes = healthy.get("attributes", {})
        assert attributes.get("components", {}).get("storage") == "error", attributes
        assert attributes.get("error_count") == 1, attributes
        status = my_predbat.dashboard_values.get("predbat.status", {})
        assert "component errors: Storage" in status.get("state", ""), status
    finally:
        my_predbat.components = original_components
        my_predbat.had_errors = original_had_errors
    return False


def test_components_all(my_predbat):
    """Run all components.py tests"""
    tests = [
        ("every_component_class_imports", test_every_component_class_imports, "every COMPONENT_LIST class imports and inherits ComponentBase"),
        ("importing_predbat_leaves_components_unloaded", test_importing_predbat_leaves_components_unloaded, "import predbat does not load the lazy component modules"),
        ("import_failure_is_an_error_not_disabled", test_import_failure_is_an_error_not_disabled, "an ImportError from a component module leaves it inactive and recorded as an error"),
        ("broken_component_does_not_take_the_others_down", test_broken_component_does_not_take_the_others_down, "a constructor exception is contained and the rest of the phase initialises"),
        ("load_error_is_reported_as_a_component_error", test_load_error_is_reported_as_a_component_error, "record_final_run_status() shows a load failure as a component error"),
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
