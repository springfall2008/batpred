# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from unittest.mock import MagicMock, patch
from predbat import PredBat

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