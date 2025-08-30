# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


from gecloud import GECloudDirect
from ohme import OhmeAPI
from octopus import OctopusAPI
from web import WebInterface

COMPONENT_LIST = {
    "web": {"class": WebInterface, "name": "Web Interface", "args": {"port": {"required": False, "config": "web_port", "default": 5052}}},
    "gecloud": {
        "class": GECloudDirect,
        "name": "GivEnergy Cloud Direct",
        "event_filter": "predbat_gecloud_",
        "args": {
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
        },
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
    },
}


class Components:
    def __init__(self, base):
        self.components = {}
        self.component_tasks = {}
        self.base = base
        self.log = base.log

    def start(self):
        for component_name, component_info in COMPONENT_LIST.items():
            have_all_args = True
            self.components[component_name] = None
            self.component_tasks[component_name] = None

            # Check required arguments
            arg_dict = {}
            for arg, arg_info in component_info["args"].items():
                required = arg_info.get("required", False)
                default = arg_info.get("default", None)
                if required and self.base.get_arg(arg_info["config"], None, indirect=False) is None:
                    have_all_args = False
                else:
                    arg_dict[arg] = self.base.get_arg(arg_info["config"], default, indirect=False)
            if have_all_args:
                self.log(f"Starting {component_info['name']} interface")
                self.components[component_name] = component_info["class"](*arg_dict.values(), self.base)
                self.component_tasks[component_name] = self.base.create_task(self.components[component_name].start())
                if not self.components[component_name].wait_api_started():
                    self.log(f"Error: {component_info['name']} API failed to start")
                    self.record_status(f"Error: {component_info['name']} API failed to start")
                    raise ValueError(f"{component_info['name']} API failed to start")

    async def stop(self):
        for component_name, component in self.components.items():
            if component:
                self.log(f"Stopping {component_name} interface")
                await component.stop()
                self.component_tasks[component_name] = None
                self.components[component_name] = None

    """
    Pass through events to the appropriate component
    """

    async def select_event(self, entity_id, value):
        for component_name, component in self.components.items():
            if component and COMPONENT_LIST[component_name].get("event_filter", "") in entity_id:
                await component.select_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        for component_name, component in self.components.items():
            if component and COMPONENT_LIST[component_name].get("event_filter", "") in entity_id:
                await component.switch_event(entity_id, service)

    async def number_event(self, entity_id, value):
        for component_name, component in self.components.items():
            if component and COMPONENT_LIST[component_name].get("event_filter", "") in entity_id:
                await component.number_event(entity_id, value)

    def is_all_alive(self):
        """Check if a component is alive, or check if all are alive"""
        return all(self.is_alive(name) for name in self.components.keys())

    def is_alive(self, name):
        """Check if a single component is alive"""
        if name not in self.components:
            return False
        if not self.component_tasks[name] or not self.component_tasks[name].is_alive():
            return False
        return self.components[name].is_alive()

    def get_active(self):
        active_components = [name for name, comp in self.components.items() if comp]
        return active_components

    def get_component(self, name):
        return self.components.get(name, None)

    def get_all(self):
        all_components = [name for name in self.components.keys()]
        return all_components
