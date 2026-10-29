# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock, patch
from predbat import PredBat
from plugin_system import PluginSystem


def test_plugin_startup_order(my_predbat):
    """
    Test that plugins are initialized before the web server starts
    This ensures plugin endpoints can be registered before the router freezes
    """
    print("*** Running test: Plugin startup order and endpoint registration")
    failed = 0

    # Create a mock for tracking call order
    call_order = []

    # Mock the Components class
    mock_components = MagicMock()
    mock_components.initialize = MagicMock(side_effect=lambda **kwargs: call_order.append("components.initialize"))
    mock_components.start = MagicMock(side_effect=lambda **kwargs: call_order.append("components.start") or True)

    # Mock the PluginSystem class
    mock_plugin_system = MagicMock()
    mock_plugin_system.discover_plugins = MagicMock(side_effect=lambda: call_order.append("plugin.discover"))
    mock_plugin_system.call_hooks = MagicMock(side_effect=lambda hook: call_order.append(f"plugin.hook.{hook}"))

    # Test the initialization order
    # Patch needs to return a callable that returns the mock
    with patch("predbat.Components", MagicMock(return_value=mock_components)):
        with patch("predbat.PluginSystem", MagicMock(return_value=mock_plugin_system)):
            # Create a minimal predbat instance for testing
            test_predbat = PredBat()
            test_predbat.reset()
            test_predbat.log = MagicMock()
            test_predbat.reset = MagicMock()
            test_predbat.auto_config = MagicMock()
            test_predbat.load_user_config = MagicMock()
            test_predbat.create_test_elements = MagicMock()
            test_predbat.expose_config = MagicMock()
            test_predbat.run_time_loop = MagicMock()
            test_predbat.ha_interface = MagicMock()
            test_predbat.publish_last_started = MagicMock()
            test_predbat.prefix = "test"
            test_predbat.had_errors = False
            test_predbat.dashboard_index = []
            test_predbat.dashboard_values = {}
            test_predbat.args = {}

            print("ha_interface = ", test_predbat.ha_interface)

            # Clear the call order
            call_order = []

            # Run the initialization
            try:
                test_predbat.update_time()
                test_predbat.initialize()
            except Exception as e:
                # Some exceptions are expected since we're mocking heavily
                print(f"Exception: {e}")
                return 1

    # Verify the order
    # Components should be initialized, then plugins discovered, then components started
    components_init_index = -1
    plugin_discover_index = -1
    components_start_index = -1

    for i, call in enumerate(call_order):
        if call == "components.initialize" and components_init_index == -1:
            components_init_index = i
        elif call == "plugin.discover" and plugin_discover_index == -1:
            plugin_discover_index = i
        elif call == "components.start" and components_start_index == -1:
            components_start_index = i

    if components_init_index == -1:
        print("ERROR: Components.initialize was not called during initialization")
        failed = 1
    elif plugin_discover_index == -1:
        print("ERROR: Plugin discovery was not called during initialization")
        failed = 1
    elif components_start_index == -1:
        print("ERROR: Components.start was not called during initialization")
        failed = 1
    elif components_init_index >= plugin_discover_index:
        print(f"ERROR: Components must be initialized (index {components_init_index}) before plugin discovery (index {plugin_discover_index})")
        print(f"Call order was: {call_order}")
        failed = 1
    elif plugin_discover_index >= components_start_index:
        print(f"ERROR: Plugin discovery (index {plugin_discover_index}) must happen before components.start (index {components_start_index})")
        print(f"Call order was: {call_order}")
        failed = 1
    else:
        print(f"OK: Correct startup order - Components.initialize ({components_init_index}) -> Plugin.discover ({plugin_discover_index}) -> Components.start ({components_start_index})")

    # Now test that a plugin can register an endpoint
    print("*** Testing plugin endpoint registration")

    # Create a mock web component
    mock_web = MagicMock()
    mock_web.registered_endpoints = []
    mock_web.register_endpoint = MagicMock(side_effect=lambda path, handler, method: mock_web.registered_endpoints.append({"path": path, "handler": handler, "method": method}))

    # Create mock components that returns our web component
    mock_components_with_web = MagicMock()
    mock_components_with_web.get_component = MagicMock(return_value=mock_web)

    # Create a test plugin that registers an endpoint
    class TestPlugin:
        def __init__(self, base):
            self.base = base

        def register_hooks(self, plugin_system):
            # Register endpoint immediately like the metrics plugin now does
            if hasattr(self.base, "components"):
                web = self.base.components.get_component("web")
                if web:
                    web.register_endpoint("/test", lambda: "test", "GET")

    # Test the plugin registration
    test_base = MagicMock()
    test_base.components = mock_components_with_web
    test_plugin = TestPlugin(test_base)
    test_plugin.register_hooks(None)

    # Verify the endpoint was registered
    if len(mock_web.registered_endpoints) == 0:
        print("ERROR: Plugin failed to register endpoint")
        failed = 1
    elif mock_web.registered_endpoints[0]["path"] != "/test":
        print(f"ERROR: Wrong endpoint path registered: {mock_web.registered_endpoints[0]['path']}")
        failed = 1
    else:
        print(f"OK: Plugin successfully registered endpoint /test")

    return failed


def test_plugin_discovery_skips_imported_base_class(my_predbat):
    """
    Discovery must instantiate the class DEFINED in the plugin file, not a class
    it merely imports.

    inspect.getmembers() returns members alphabetically and the name-based strategy
    takes the first class ending in "Plugin". Every plugin does
    `from plugin_system import PredBatPlugin`, so any plugin class sorting AFTER
    "PredBatPlugin" had the abstract base instantiated instead of itself — the
    plugin logged as loaded successfully, registered no hooks, and did nothing.
    """
    print("*** Running test: Plugin discovery skips imported base classes")
    failed = 0

    base = MagicMock()
    base.log = MagicMock()

    tmpdir = tempfile.mkdtemp()
    try:
        # Class name deliberately sorts AFTER "PredBatPlugin".
        with open(os.path.join(tmpdir, "zz_sorts_last_plugin.py"), "w", encoding="utf-8") as fh:
            fh.write("from plugin_system import PredBatPlugin\n\n\nclass ZzSortsLastPlugin(PredBatPlugin):\n    pass\n")

        plugin_system = PluginSystem(base)
        plugin_system.discover_plugins([tmpdir])
        loaded = list(plugin_system.plugins.values())

        if len(loaded) != 1:
            print("ERROR: expected exactly one discovered plugin, got {}".format(len(loaded)))
            failed = 1
        elif type(loaded[0]).__name__ != "ZzSortsLastPlugin":
            print("ERROR: discovery instantiated {}, expected ZzSortsLastPlugin".format(type(loaded[0]).__name__))
            failed = 1
        else:
            print("OK: discovery instantiated the plugin class, not the imported base")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed


def test_plugin_discovery_fallback_skips_imported_base_class(my_predbat):
    """
    The SAME guard must hold on the attribute-based fallback (strategy 2).

    PredBatPlugin sets PREDBAT_PLUGIN = True as its own discovery marker, so it
    matches strategy 2 in every plugin that imports it. Guarding only the
    name-based strategy leaves the identical bug reachable whenever strategy 1
    declines — here the plugin class does not end in "Plugin", so discovery falls
    straight through to the fallback and, unguarded, constructs the abstract base:
    plugin logged as loaded, no hooks registered, silently does nothing.
    """
    print("*** Running test: Plugin discovery fallback skips imported base classes")
    failed = 0

    base = MagicMock()
    base.log = MagicMock()

    tmpdir = tempfile.mkdtemp()
    try:
        # Three properties here are all load-bearing:
        #   - the FILE ends "_plugin.py", or discover_plugins never reads it;
        #   - the CLASS does not end in "Plugin", so strategy 1 finds nothing and
        #     discovery falls through to the PREDBAT_PLUGIN attribute match;
        #   - the CLASS sorts AFTER "PredBatPlugin", because inspect.getmembers()
        #     is alphabetical and strategy 2 breaks on its first match. A name
        #     sorting before the base hides the bug entirely - which is how the
        #     first draft of this test passed with and without the fix.
        with open(os.path.join(tmpdir, "attribute_only_plugin.py"), "w", encoding="utf-8") as fh:
            fh.write("from plugin_system import PredBatPlugin\n\n\nclass ZzAttributeWorker(PredBatPlugin):\n    pass\n")

        plugin_system = PluginSystem(base)
        plugin_system.discover_plugins([tmpdir])
        loaded = list(plugin_system.plugins.values())

        if len(loaded) != 1:
            print("ERROR: expected exactly one discovered plugin, got {}".format(len(loaded)))
            failed = 1
        elif type(loaded[0]).__name__ != "ZzAttributeWorker":
            print("ERROR: fallback instantiated {}, expected ZzAttributeWorker".format(type(loaded[0]).__name__))
            failed = 1
        else:
            print("OK: attribute-based fallback instantiated the plugin class, not the imported base")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed


def test_plugin_discovery_does_not_retry_a_failed_class(my_predbat):
    """
    A plugin class that raises in __init__ must be constructed ONCE, not twice.

    The class ends in "Plugin" and inherits PREDBAT_PLUGIN from the base, so it
    matches both strategy 1 and strategy 2. Strategy 1 tries it and it raises;
    unless strategy 2 skips classes strategy 1 already attempted, it constructs
    the same class again, and any side effect before the raising line (a hook
    registered, state written) happens twice.
    """
    print("*** Running test: Plugin discovery does not retry a class that failed")
    failed = 0

    base = MagicMock()
    base.log = MagicMock()

    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, "zz_raises_plugin.py"), "w", encoding="utf-8") as fh:
            fh.write("from plugin_system import PredBatPlugin\n\n\nclass ZzRaisesPlugin(PredBatPlugin):\n    def __init__(self, base):\n        raise ValueError('bad config')\n")

        plugin_system = PluginSystem(base)
        plugin_system.discover_plugins([tmpdir])
        attempts = [c for c in base.log.call_args_list if "Initialising plugin class" in str(c) and "ZzRaisesPlugin" in str(c)]

        if len(attempts) != 1:
            print("ERROR: ZzRaisesPlugin was constructed {} times, expected 1".format(len(attempts)))
            failed = 1
        elif plugin_system.plugins:
            print("ERROR: expected no plugin loaded, got {}".format(list(plugin_system.plugins)))
            failed = 1
        else:
            print("OK: the failing plugin class was constructed once and not retried")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed


def test_plugin_discovery_names_a_reexported_class(my_predbat):
    """
    A plugin class imported into the _plugin.py file, rather than defined there, is
    not loaded, and the log must say which class was skipped and where it came from.

    Discovery only takes classes defined in the plugin file, so a layout that keeps
    the class in a helper module and re-exports it does not load. Without naming
    the skipped class, the only log line is "No plugin class ... found", which
    points nowhere.
    """
    print("*** Running test: Plugin discovery names a re-exported plugin class")
    failed = 0

    base = MagicMock()
    base.log = MagicMock()

    tmpdir = tempfile.mkdtemp()
    sys.path.insert(0, tmpdir)
    try:
        with open(os.path.join(tmpdir, "zz_reexport_core.py"), "w", encoding="utf-8") as fh:
            fh.write("from plugin_system import PredBatPlugin\n\n\nclass ZzReexportPlugin(PredBatPlugin):\n    pass\n")
        with open(os.path.join(tmpdir, "zz_reexport_plugin.py"), "w", encoding="utf-8") as fh:
            fh.write("from plugin_system import PredBatPlugin\nfrom zz_reexport_core import ZzReexportPlugin\n")

        plugin_system = PluginSystem(base)
        plugin_system.discover_plugins([tmpdir])
        messages = [str(c.args[0]) for c in base.log.call_args_list if c.args]
        expected = "Plugin classes in zz_reexport_plugin that are imported, not defined there, are not loaded: ZzReexportPlugin (from zz_reexport_core)"

        if plugin_system.plugins:
            print("ERROR: expected no plugin loaded, got {}".format(list(plugin_system.plugins)))
            failed = 1
        elif expected not in messages:
            print("ERROR: expected log line not found: {}".format(expected))
            failed = 1
        else:
            print("OK: the skipped re-exported class is named in the log")
    finally:
        sys.path.remove(tmpdir)
        sys.modules.pop("zz_reexport_core", None)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed
