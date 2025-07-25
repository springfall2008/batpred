# -----------------------------------------------------------------------------
# Predbat Home Battery System - Plugin System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import os
import importlib
import importlib.util
import inspect
import sys
from typing import Dict, List, Callable, Any


class PluginSystem:
    """
    Plugin discovery and management system for Predbat
    """

    def __init__(self, base):
        self.base = base
        self.log = base.log
        self.plugins = {}
        self.hooks = {"on_init": [], "on_update": [], "on_shutdown": [], "on_web_start": []}

    def register_hook(self, hook_name: str, callback: Callable):
        """
        Register a callback for a specific hook

        Args:
            hook_name (str): Name of the hook ('on_init', 'on_update', 'on_shutdown', 'on_web_start')
            callback (Callable): Function to call when hook is triggered
        """
        if hook_name not in self.hooks:
            self.hooks[hook_name] = []

        self.hooks[hook_name].append(callback)
        self.log(f"Registered hook: {hook_name} -> {callback.__name__}")

    def call_hooks(self, hook_name: str, *args, **kwargs):
        """
        Call all registered callbacks for a specific hook

        Args:
            hook_name (str): Name of the hook to call
            *args, **kwargs: Arguments to pass to the callbacks
        """
        if hook_name in self.hooks:
            for callback in self.hooks[hook_name]:
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    self.log(f"Error calling hook {hook_name} callback {callback.__name__}: {e}")

    def discover_plugins(self, plugin_dirs: List[str] = None):
        """
        Auto-discover plugins in specified directories

        Args:
            plugin_dirs (List[str]): List of directories to search for plugins
        """
        if plugin_dirs is None:
            # Default plugin directories
            plugin_dirs = [
                os.path.dirname(__file__),  # Same directory as predbat files
                os.path.join(os.path.dirname(__file__), "plugins"),  # plugins subdirectory
                os.path.join(os.path.dirname(__file__), "..", "plugins"),  # plugins in parent
            ]

        discovered_count = 0

        for plugin_dir in plugin_dirs:
            if not os.path.exists(plugin_dir):
                continue

            self.log(f"Scanning for plugins in: {plugin_dir}")

            for filename in os.listdir(plugin_dir):
                if filename.endswith("_plugin.py"):
                    plugin_name = filename[:-3]  # Remove .py extension

                    try:
                        self.load_plugin(plugin_dir, plugin_name)
                        discovered_count += 1
                    except Exception as e:
                        self.log(f"Failed to load plugin {plugin_name}: {e}")

        self.log(f"Plugin discovery complete. Loaded {discovered_count} plugins.")

    def load_plugin(self, plugin_dir: str, plugin_name: str):
        """
        Load a specific plugin from a directory

        Args:
            plugin_dir (str): Directory containing the plugin
            plugin_name (str): Name of the plugin module (without .py)
        """
        plugin_path = os.path.join(plugin_dir, f"{plugin_name}.py")

        if not os.path.exists(plugin_path):
            return

        # Import the plugin module
        spec = importlib.util.spec_from_file_location(plugin_name, plugin_path)
        if spec is None:
            return

        plugin_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin_module)

        # Look for plugin classes or initialization functions
        plugin_instance = None

        # Look for classes that might be plugins
        for name, obj in inspect.getmembers(plugin_module, inspect.isclass):
            if name.endswith("Plugin") or name.endswith("Metrics") or hasattr(obj, "PREDBAT_PLUGIN"):
                try:
                    self.log(f"Initializing plugin class: {name}")
                    plugin_instance = obj(self.base)
                    break
                except Exception as e:
                    self.log(f"Failed to initialize plugin class {name}: {e}")

        # Look for plugin initialization function
        if plugin_instance is None and hasattr(plugin_module, "initialize_plugin"):
            try:
                self.log(f"Calling initialize_plugin function in {plugin_name}")
                plugin_instance = plugin_module.initialize_plugin(self.base)
            except Exception as e:
                self.log(f"Failed to call initialize_plugin in {plugin_name}: {e}")

        # Look for auto-initialization based on class names containing certain keywords
        if plugin_instance is None:
            for name, obj in inspect.getmembers(plugin_module, inspect.isclass):
                if any(keyword in name.lower() for keyword in ["predbat", "plugin", "metrics", "monitor"]):
                    try:
                        self.log(f"Auto-initializing plugin class: {name}")
                        plugin_instance = obj(self.base)
                        break
                    except Exception as e:
                        self.log(f"Failed to auto-initialize plugin class {name}: {e}")

        if plugin_instance:
            self.plugins[plugin_name] = plugin_instance
            self.log(f"Successfully loaded plugin: {plugin_name}")

            # Call the plugin's registration hooks if it has them
            if hasattr(plugin_instance, "register_hooks"):
                try:
                    plugin_instance.register_hooks(self)
                except Exception as e:
                    self.log(f"Failed to register hooks for plugin {plugin_name}: {e}")
        else:
            self.log(f"No plugin class or initialization function found in {plugin_name}")

    def get_plugin(self, plugin_name: str):
        """
        Get a loaded plugin by name

        Args:
            plugin_name (str): Name of the plugin

        Returns:
            Plugin instance or None if not found
        """
        return self.plugins.get(plugin_name)

    def list_plugins(self):
        """
        Get list of loaded plugin names

        Returns:
            List[str]: List of plugin names
        """
        return list(self.plugins.keys())

    def shutdown_plugins(self):
        """
        Shutdown all plugins gracefully
        """
        self.call_hooks("on_shutdown")

        for plugin_name, plugin in self.plugins.items():
            if hasattr(plugin, "shutdown"):
                try:
                    plugin.shutdown()
                    self.log(f"Shutdown plugin: {plugin_name}")
                except Exception as e:
                    self.log(f"Error shutting down plugin {plugin_name}: {e}")


# Plugin base class (optional, plugins don't have to inherit from this)
class PredBatPlugin:
    """
    Base class for Predbat plugins (optional to inherit)
    """

    PREDBAT_PLUGIN = True  # Marker for auto-discovery

    def __init__(self, base):
        self.base = base
        self.log = base.log

    def register_hooks(self, plugin_system):
        """
        Override this method to register hooks with the plugin system
        """
        pass

    def shutdown(self):
        """
        Override this method for cleanup when plugin is shutdown
        """
        pass
