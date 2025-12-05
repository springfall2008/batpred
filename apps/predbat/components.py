# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


from solcast import SolarAPI
from gecloud import GECloudDirect, GECloudData
from ohme import OhmeAPI
from octopus import OctopusAPI
from carbon import CarbonAPI
from alertfeed import AlertFeed
from web import WebInterface
from ha import HAInterface, HAHistory
from db_manager import DatabaseManager
from fox import FoxAPI
from web_mcp import PredbatMCPServer
from datetime import datetime, timezone, timedelta
import asyncio
import os


COMPONENT_LIST = {
    "db": {
        "class": DatabaseManager,
        "name": "Database Manager",
        "args": {
            "db_enable": {"required": True, "config": "db_enable"},
            "db_days": {"required": False, "config": "db_days", "default": 30},
        },
        "can_restart": False,
        "phase": 0,
        "new": True,
    },
    "ha": {
        "class": HAInterface,
        "name": "Home Assistant Interface",
        "args": {
            "ha_url": {"required": False, "config": "ha_url", "default": "http://supervisor/core"},
            "ha_key": {"required": False, "config": "ha_key", "default": os.environ.get("SUPERVISOR_TOKEN", None)},
            "db_enable": {"required": False, "config": "db_enable", "default": False},
            "db_mirror_ha": {"required": False, "config": "db_mirror_ha", "default": False},
            "db_primary": {"required": False, "config": "db_primary", "default": False},
        },
        "can_restart": False,
        "phase": 0,
    },
    "ha_history": {"class": HAHistory, "name": "Home Assistant History", "args": {}, "can_restart": False, "phase": 0, "new": True},
    "web": {
        "class": WebInterface,
        "name": "Web Interface",
        "args": {
            "web_port": {"required": False, "config": "web_port", "default": 5052},
        },
        "phase": 0,
    },
    "mcp": {
        "class": PredbatMCPServer,
        "name": "MCP Server",
        "args": {
            "mcp_enable": {"required": True, "config": "mcp_enable", "default": False},
            "mcp_secret": {"required": False, "config": "mcp_secret", "default": "predbat_mcp_secret"},
            "mcp_port": {"required": False, "config": "mcp_port", "default": 8199},
        },
        "phase": 1,
    },
    "solar": {
        "class": SolarAPI,
        "name": "Solar API",
        "args": {
            "solcast_host": {"required": False, "config": "solcast_host", "default": "https://api.solcast.com.au/"},
            "solcast_api_key": {"required": False, "config": "solcast_api_key"},
            "solcast_sites": {"required": False, "config": "solcast_sites"},
            "solcast_poll_hours": {"required": False, "config": "solcast_poll_hours", "default": 8},
            "forecast_solar": {"required": False, "config": "forecast_solar", "default": False},
            "forecast_solar_max_age": {"required": False, "config": "forecast_solar_max_age", "default": 8},
            "pv_forecast_today": {"required": False, "config": "pv_forecast_today"},
            "pv_forecast_tomorrow": {"required": False, "config": "pv_forecast_tomorrow"},
            "pv_forecast_d3": {"required": False, "config": "pv_forecast_d3"},
            "pv_forecast_d4": {"required": False, "config": "pv_forecast_d4"},
            "pv_scaling": {"required": False, "config": "pv_scaling", "default": 1.0},
        },
        "required_or": ["solcast_host", "forecast_solar", "pv_forecast_today"],
        "phase": 1,
    },
    "gecloud": {
        "class": GECloudDirect,
        "name": "GivEnergy Cloud Direct",
        "event_filter": "predbat_gecloud_",
        "args": {
            "ge_cloud_direct": {
                "required_true": True,
                "config": "ge_cloud_direct",
            },
            "api_key": {
                "required": True,
                "config": "ge_cloud_key",
            },
            "automatic": {
                "required": False,
                "default": False,
                "config": "ge_cloud_automatic",
            },
        },
        "phase": 1,
    },
    "gecloud_data": {
        "class": GECloudData,
        "name": "GivEnergy Cloud Data",
        "args": {
            "ge_cloud_data": {
                "required_true": True,
                "config": "ge_cloud_data",
            },
            "ge_cloud_key": {
                "required": True,
                "config": "ge_cloud_key",
            },
            "ge_cloud_serial": {
                "config": "ge_cloud_serial",
                "config_late_resolve": True,
            },
            "days_previous": {
                "required": False,
                "default": [7],
                "config": "days_previous",
            },
        },
        "phase": 1,
    },
    "octopus": {
        "class": OctopusAPI,
        "name": "Octopus Energy Direct",
        "event_filter": "predbat_octopus_",
        "args": {
            "key": {
                "required": True,
                "config": "octopus_api_key",
            },
            "account_id": {
                "required": True,
                "config": "octopus_api_account",
            },
            "automatic": {
                "required": False,
                "default": True,
                "config": "octopus_automatic",
            },
        },
        "phase": 1,
    },
    "ohme": {
        "class": OhmeAPI,
        "name": "Ohme Charger",
        "event_filter": "predbat_ohme_",
        "args": {
            "email": {
                "required": True,
                "config": "ohme_login",
            },
            "password": {
                "required": True,
                "config": "ohme_password",
            },
            "ohme_automatic_octopus_intelligent": {
                "required": False,
                "config": "ohme_automatic_octopus_intelligent",
            },
        },
        "phase": 1,
    },
    "fox": {
        "class": FoxAPI,
        "name": "Fox API",
        "event_filter": "predbat_fox_",
        "args": {
            "key": {
                "required": True,
                "config": "fox_key",
            },
            "automatic": {
                "required": False,
                "default": False,
                "config": "fox_automatic",
            },
        },
        "phase": 1,
    },
    "alert_feed": {
        "class": AlertFeed,
        "name": "Alert Feed",
        "event_filter": "predbat_alertfeed_",
        "args": {
            "alert_config": {
                "required": True,
                "default": {},
                "config": "alerts",
            },
        },
        "phase": 1,
    },
    "carbon": {
        "class": CarbonAPI,
        "name": "Carbon Intensity API",
        "args": {
            "postcode": {"required": True, "config": "carbon_postcode"},
            "automatic": {"required": False, "config": "carbon_automatic", "default": False},
        },
        "phase": 1,
    },
}


class Components:
    def __init__(self, base):
        self.components = {}
        self.component_tasks = {}
        self.base = base
        self.log = base.log

    def initialize(self, only=None, phase=0):
        """Initialize components without starting them"""
        for component_name, component_info in COMPONENT_LIST.items():
            if only and component_name != only:
                continue
            if component_info.get("phase", 0) != phase:
                continue

            have_all_args = True
            self.components[component_name] = None
            self.component_tasks[component_name] = None

            # Check required arguments
            arg_dict = {}
            for arg, arg_info in component_info["args"].items():
                required = arg_info.get("required", False)
                required_true = arg_info.get("required_true", False)
                default = arg_info.get("default", None)
                indirect = arg_info.get("indirect", False)
                config_late_resolve = arg_info.get("config_late_resolve", False)
                if config_late_resolve:
                    # Defer resolution of config value until later
                    arg_dict[arg] = arg_info["config"]
                    continue
                elif required_true and not self.base.get_arg(arg_info["config"], False, indirect=False):
                    have_all_args = False
                elif required and self.base.get_arg(arg_info["config"], None, indirect=False) is None:
                    have_all_args = False
                else:
                    arg_dict[arg] = self.base.get_arg(arg_info["config"], default, indirect=indirect)
            required_or = component_info.get("required_or", [])
            # If required_or is set we must have at least one of the listed args
            if required_or:
                if not any(arg_dict.get(arg, None) for arg in required_or):
                    have_all_args = False
            if have_all_args:
                self.log(f"Initializing {component_info['name']} interface")
                self.components[component_name] = component_info["class"](self.base, **arg_dict)

    def start(self, only=None, phase=0):
        """Start all initialized components"""
        failed = False
        for component_name, component_info in COMPONENT_LIST.items():
            if only and component_name != only:
                continue
            component = self.components.get(component_name)
            if component:
                if component_info.get("phase", 0) != phase:
                    continue
                if self.component_tasks.get(component_name, None) and self.component_tasks[component_name].is_alive():
                    self.log(f"Info: {component_info['name']} interface already started")
                    continue
                elif self.component_tasks.get(component_name, None):
                    self.log(f"Info: {component_info['name']} interface task not alive, restarting")
                else:
                    self.log(f"Starting {component_info['name']} interface")

                # Create new task
                self.component_tasks[component_name] = self.base.create_task(component.start())
                if not component.wait_api_started():
                    self.log(f"Error: {component_info['name']} API failed to start")
                    failed = True
        return not failed

    async def stop(self, only=None):
        for component_name, component_info in reversed(list(self.components.items())):
            if only and component_name != only:
                continue
            component = self.components[component_name]
            if component:
                self.log(f"Stopping {component_name} interface")
                await component.stop()
                self.log(f"Stopped {component_name} interface")
                self.component_tasks[component_name] = None
                self.components[component_name] = None

    async def restart(self, only):
        """Restart components"""
        # Check can restart
        if not self.can_restart(only):
            self.log(f"Warn: Restarting component {only} is not supported")
            return
        self.log(f"Restarting {only} component")
        await self.stop(only=only)
        self.log("Waiting 10 seconds before restarting component(s)")
        await asyncio.sleep(10)
        self.log("Starting component(s) again")
        self.initialize(only=only)
        self.start(only=only)

    """
    Pass through events to the appropriate component
    """

    async def select_event(self, entity_id, value):
        for component_name, component in self.components.items():
            event_filter = COMPONENT_LIST[component_name].get("event_filter", None)
            if component and event_filter and (event_filter in entity_id):
                await component.select_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        for component_name, component in self.components.items():
            event_filter = COMPONENT_LIST[component_name].get("event_filter", None)
            if component and event_filter and (event_filter in entity_id):
                await component.switch_event(entity_id, service)

    async def number_event(self, entity_id, value):
        for component_name, component in self.components.items():
            event_filter = COMPONENT_LIST[component_name].get("event_filter", None)
            if component and event_filter and (event_filter in entity_id):
                await component.number_event(entity_id, value)

    def is_all_alive(self):
        """Check if a component is alive, or check if all are alive"""
        return all(self.is_alive(name) for name in self.components.keys())

    def is_alive(self, name):
        """Check if a single component is alive"""
        if name not in self.components:
            return False
        if not self.components[name]:
            # Disabled components can be ignored
            return True
        if not self.component_tasks[name] or not self.component_tasks[name].is_alive():
            return False
        if not self.components[name].is_alive():
            return False
        last_updated_time = self.last_updated_time(name)
        diff_time = datetime.now(timezone.utc) - last_updated_time if last_updated_time else None
        if not diff_time or diff_time > timedelta(minutes=60):
            return False
        return True

    def last_updated_time(self, name):
        """Get last successful update time for a component"""
        if name not in self.components:
            return None
        if not self.components[name]:
            return None
        if "last_updated_time" not in dir(self.components[name]):
            return None
        return self.components[name].last_updated_time()

    def get_active(self):
        active_components = [name for name, comp in self.components.items() if comp]
        return active_components

    def get_component(self, name):
        return self.components.get(name, None)

    def get_all(self):
        all_components = [name for name in self.components.keys()]
        return all_components

    def can_restart(self, name):
        """Check if a component can be restarted"""
        if name not in COMPONENT_LIST:
            return False
        return COMPONENT_LIST[name].get("can_restart", True)
