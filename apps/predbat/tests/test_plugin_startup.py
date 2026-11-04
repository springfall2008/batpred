# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import importlib.util
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock, patch
from predbat import PredBat
import plugin_system as plugin_system_module
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


def _discover(files):
    """
    Write files into a temporary plugin directory, run discovery and return (plugin_system, base, log messages).

    The directory is on sys.path while discovery runs, so a plugin can import a helper written next to it.
    """
    base = MagicMock()
    base.log = MagicMock()
    tmpdir = tempfile.mkdtemp()
    sys.path.insert(0, tmpdir)
    try:
        for filename, content in files.items():
            with open(os.path.join(tmpdir, filename), "w", encoding="utf-8") as fh:
                fh.write(content)
        plugin_system = PluginSystem(base)
        plugin_system.discover_plugins([tmpdir])
    finally:
        sys.path.remove(tmpdir)
        for filename in files:
            sys.modules.pop(filename[:-3], None)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return plugin_system, base, [str(c.args[0]) for c in base.log.call_args_list if c.args]


def _loaded_class_names(plugin_system):
    """Return the class name of every plugin that was loaded."""
    return [type(plugin).__name__ for plugin in plugin_system.plugins.values()]


BASE_IMPORT = "from plugin_system import PredBatPlugin\n\n\n"


def test_plugin_discovery_does_not_retry_a_failed_class(my_predbat):
    """
    A plugin class that raises in __init__ must be constructed ONCE, not twice.

    The class matches on both its name and the inherited PREDBAT_PLUGIN marker. If
    the two matches were tried separately, any side effect before the raising line
    (a hook registered, state written) would happen twice.
    """
    print("*** Running test: Plugin discovery does not retry a class that failed")
    plugin_system, base, _ = _discover({"zz_raises_plugin.py": BASE_IMPORT + "class ZzRaisesPlugin(PredBatPlugin):\n    def __init__(self, base):\n        base.note_constructed()\n        raise ValueError('bad config')\n"})

    if base.note_constructed.call_count != 1:
        print("ERROR: ZzRaisesPlugin was constructed {} times, expected 1".format(base.note_constructed.call_count))
        return 1
    if plugin_system.plugins:
        print("ERROR: expected no plugin loaded, got {}".format(list(plugin_system.plugins)))
        return 1
    print("OK: the failing plugin class was constructed once and not retried")
    return 0


def test_plugin_discovery_loads_a_reexported_class(my_predbat):
    """
    A plugin class kept in a helper module and imported into the _plugin.py file must load, wherever it sorts against PredBatPlugin.
    """
    print("*** Running test: Plugin discovery loads a re-exported plugin class")
    failed = 0
    for class_name in ("AlphaReexportPlugin", "ZzReexportPlugin"):
        plugin_system, _, _ = _discover(
            {
                "reexport_core.py": BASE_IMPORT + "class {}(PredBatPlugin):\n    pass\n".format(class_name),
                "reexport_plugin.py": "from plugin_system import PredBatPlugin\nfrom reexport_core import {}\n".format(class_name),
            }
        )
        if _loaded_class_names(plugin_system) != [class_name]:
            print("ERROR: expected {} to load, got {}".format(class_name, _loaded_class_names(plugin_system)))
            failed = 1
        else:
            print("OK: re-exported {} loaded".format(class_name))
    return failed


def test_plugin_discovery_skips_a_local_base_class(my_predbat):
    """
    A plugin's own intermediate base class must not be chosen over the class derived from it, even though it sorts first.
    """
    print("*** Running test: Plugin discovery skips a local intermediate base class")
    plugin_system, _, _ = _discover({"foo_plugin.py": BASE_IMPORT + "class BaseFooPlugin(PredBatPlugin):\n    pass\n\n\nclass FooPlugin(BaseFooPlugin):\n    pass\n"})

    if _loaded_class_names(plugin_system) != ["FooPlugin"]:
        print("ERROR: expected FooPlugin to load, got {}".format(_loaded_class_names(plugin_system)))
        return 1
    print("OK: the most-derived class loaded, not its local base")
    return 0


def test_plugin_discovery_names_an_import_skipped_for_a_local_class(my_predbat):
    """
    When a local helper class is chosen over a re-exported class, the log must name the class that was skipped, or the real plugin silently never runs.
    """
    print("*** Running test: Plugin discovery names an import skipped for a local class")
    plugin_system, _, messages = _discover(
        {
            "real_core.py": BASE_IMPORT + "class ZzRealPlugin(PredBatPlugin):\n    pass\n",
            "real_plugin.py": "from plugin_system import PredBatPlugin\nfrom real_core import ZzRealPlugin\n\n\nclass ConfigPlugin(PredBatPlugin):\n    pass\n",
        }
    )
    expected = "Plugin classes in real_plugin that are imported, not defined there, are not loaded: ZzRealPlugin (from real_core)"

    if _loaded_class_names(plugin_system) != ["ConfigPlugin"]:
        print("ERROR: expected the local ConfigPlugin to load, got {}".format(_loaded_class_names(plugin_system)))
        return 1
    if expected not in messages:
        print("ERROR: expected log line not found: {}".format(expected))
        return 1
    print("OK: the skipped import is named in the log")
    return 0


def test_plugin_discovery_reports_a_constructor_failure(my_predbat):
    """
    A class that was found but raised must be reported as that, not as "No plugin class found", which contradicts the failure logged just before it.
    """
    print("*** Running test: Plugin discovery reports a constructor failure")
    _, _, messages = _discover({"zz_raises_plugin.py": BASE_IMPORT + "class ZzRaisesPlugin(PredBatPlugin):\n    def __init__(self, base):\n        raise ValueError('bad config')\n"})
    expected = "Plugin zz_raises_plugin was found but could not be initialised, see the errors above"
    wrong = "No plugin class or initialisation function found in zz_raises_plugin"

    if expected not in messages:
        print("ERROR: expected log line not found: {}".format(expected))
        return 1
    if wrong in messages:
        print("ERROR: log still says: {}".format(wrong))
        return 1
    print("OK: the constructor failure is reported as a failure")
    return 0


def test_plugin_discovery_reports_nothing_found(my_predbat):
    """
    A plugin file offering only the imported base class has nothing to load, and the log must say so rather than constructing the base.
    """
    print("*** Running test: Plugin discovery reports nothing found")
    plugin_system, _, messages = _discover({"empty_plugin.py": "from plugin_system import PredBatPlugin\n"})
    expected = "No plugin class or initialisation function found in empty_plugin"

    if plugin_system.plugins:
        print("ERROR: expected no plugin loaded, got {}".format(_loaded_class_names(plugin_system)))
        return 1
    if expected not in messages:
        print("ERROR: expected log line not found: {}".format(expected))
        return 1
    print("OK: nothing was loaded and the log says so")
    return 0


def test_plugin_discovery_keeps_import_out_after_local_failure(my_predbat):
    """
    When the class defined in the file raises, discovery must not fall through to an imported class: that would run something the author never asked to load.
    """
    print("*** Running test: Plugin discovery does not fall through to an import after a local failure")
    plugin_system, base, _ = _discover(
        {
            "other_core.py": BASE_IMPORT + "class OtherPlugin(PredBatPlugin):\n    def __init__(self, base):\n        base.note_import_constructed()\n",
            "mine_plugin.py": "from plugin_system import PredBatPlugin\nfrom other_core import OtherPlugin\n\n\nclass MinePlugin(PredBatPlugin):\n    def __init__(self, base):\n        raise ValueError('bad config')\n",
        }
    )

    if base.note_import_constructed.call_count != 0:
        print("ERROR: the imported class was constructed {} times, expected 0".format(base.note_import_constructed.call_count))
        return 1
    if plugin_system.plugins:
        print("ERROR: expected no plugin loaded, got {}".format(_loaded_class_names(plugin_system)))
        return 1
    print("OK: the imported class was left alone")
    return 0


def test_plugin_discovery_handles_base_under_another_import_name(my_predbat):
    """
    If plugin_system is imported under a second name the plugin's PredBatPlugin is a different class object, which must still not be treated as a re-exported plugin class.

    The local class raises so that discovery reports what it skipped: a working local class would hide the old behaviour.
    """
    print("*** Running test: Plugin discovery handles the base under another import name")
    alias = "zz_alias_plugin_system"
    spec = importlib.util.spec_from_file_location(alias, plugin_system_module.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[alias] = module
    try:
        _, _, messages = _discover({"alias_plugin.py": "from {} import PredBatPlugin\n\n\nclass AliasPlugin(PredBatPlugin):\n    def __init__(self, base):\n        raise ValueError('bad config')\n".format(alias)})
    finally:
        sys.modules.pop(alias, None)

    wrongly_skipped = [message for message in messages if "that are imported, not defined there" in message]
    if wrongly_skipped:
        print("ERROR: the base class was reported as a skipped import: {}".format(wrongly_skipped))
        return 1
    if "Failed to initialise plugin class AliasPlugin: bad config" not in messages:
        print("ERROR: AliasPlugin was never tried, so this test did not reach its subject")
        return 1
    print("OK: the base class under another import name was not reported as an import")
    return 0
