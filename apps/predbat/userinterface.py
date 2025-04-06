# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
from datetime import datetime, timedelta
import json
import yaml
import re
import copy
from config import (
    TIME_FORMAT,
    PREDBAT_MODE_OPTIONS,
    THIS_VERSION,
    CONFIG_API_OVERRIDE,
    PREDBAT_MODE_MONITOR,
    PREDBAT_MODE_CONTROL_SOC,
    PREDBAT_MODE_CONTROL_CHARGEDISCHARGE,
    PREDBAT_MODE_CONTROL_CHARGE,
    PREDBAT_SAVE_RESTORE,
    PREDBAT_UPDATE_OPTIONS,
    PREDICT_STEP,
    CONFIG_REFRESH_PERIOD,
    CONFIG_ROOTS,
)

DEBUG_EXCLUDE_LIST = [
    "pool",
    "ha_interface",
    "web_interface",
    "web_interface_task",
    "prediction",
    "logfile",
    "predheat",
    "inverters",
    "run_list",
    "threads",
    "EVENT_LISTEN_LIST",
    "local_tz",
    "CONFIG_ITEMS",
    "config_index",
    "comparison",
    "octopus_api_direct",
    "octopus_api_direct_task",
    "ge_cloud_direct",
    "ge_cloud_direct_task",
]


class UserInterface:
    def call_notify(self, message):
        """
        Sync wrapper for call_notify
        """
        for device in self.notify_devices:
            self.call_service_wrapper("notify/" + device, message=message)
        return True

    def call_service_wrapper_stub2(self, service, message):
        """
        Stub for 2 arg service wrapper
        """
        return self.call_service_wrapper(service, message=message)

    async def async_call_notify(self, message):
        """
        Send HA notifications
        """
        for device in self.notify_devices:
            await self.run_in_executor(self.call_service_wrapper_stub2, "notify/" + device, message)
        return True

    def resolve_arg(self, arg, value, default=None, indirect=True, combine=False, attribute=None, index=None, extra_args=None, quiet=False):
        """
        Resolve argument templates and state instances
        """
        if isinstance(value, list) and (index is not None):
            if index < len(value):
                value = value[index]
            else:
                if not quiet:
                    self.log("Warn: Out of range index {} within item {} value {}".format(index, arg, value))
                value = None
            index = None

        if index:
            self.log("Warn: Out of range index {} within item {} value {}".format(index, arg, value))

        # If we have a list of items get each and add them up or return them as a list
        if isinstance(value, list):
            if combine:
                final = 0
                for item in value:
                    got = self.resolve_arg(arg, item, default=default, indirect=True)
                    try:
                        final += float(got)
                    except (ValueError, TypeError):
                        if not quiet:
                            self.log("Warn: Return bad value {} from {} arg {}".format(got, item, arg))
                            self.record_status("Warn: Return bad value {} from {} arg {}".format(got, item, arg), had_errors=True)
                return final
            else:
                final = []
                for item in value:
                    item = self.resolve_arg(arg, item, default=default, indirect=indirect)
                    if isinstance(item, list):
                        final += item
                    else:
                        final.append(item)
                return final

        # Resolve templated data
        for repeat in range(2):
            if isinstance(value, str) and "{" in value:
                try:
                    if extra_args:
                        # Remove duplicates or format will fail
                        arg_hash = {}
                        arg_hash.update(self.args)
                        arg_hash.update(extra_args)
                        value = value.format(**arg_hash)
                    else:
                        value = value.format(**self.args)
                except KeyError:
                    if not quiet:
                        self.log("Warn: can not resolve {} value {}".format(arg, value))
                        self.record_status("Warn: can not resolve {} value {}".format(arg, value), had_errors=True)
                    value = default

        # Resolve join list by name
        if isinstance(value, str) and value.startswith("+[") and value.endswith("]"):
            value = self.get_arg(value[2:-1], default=default, indirect=indirect, combine=False, attribute=attribute, index=index)

        # Resolve indirect instance
        if indirect and isinstance(value, str) and "." in value:
            if "$" in value:
                value, attribute = value.split("$")

            if attribute:
                value = self.get_state_wrapper(entity_id=value, default=default, attribute=attribute)
            else:
                value = self.get_state_wrapper(entity_id=value, default=default)
        return value

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True):
        """
        Argument getter that can use HA state as well as fixed values
        """
        value = None
        if can_override:
            can_override = CONFIG_API_OVERRIDE.get(arg, False)

        if can_override:
            overrides = self.get_manual_api(arg)
            if isinstance(default, list):
                value = self.get_arg(arg, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index, domain=domain, can_override=False)
                for override in overrides:
                    override_index = override.get("index", 0)
                    if override_index is None:
                        override_index = 0
                    for idx in range(max(len(value), override_index + 1)):
                        if override_index == idx:
                            if len(value) <= idx:
                                # Extend length of value list to match index
                                value.extend([default] * (idx - len(value) + 1))
                            org_value = value[idx]
                            value[idx] = override.get("value", None)
                            if isinstance(org_value, float):
                                try:
                                    value[idx] = float(value[idx])
                                except (ValueError, TypeError):
                                    self.log("Warn: Return bad float value {} from {} override using default {}".format(value[idx], arg, default))
                                    self.record_status("Warn: Return bad float value {} from arg override {}".format(value[idx], arg), had_errors=True)
                                    value[idx] = default
                            elif isinstance(org_value, int) and not isinstance(org_value, bool):
                                try:
                                    value[idx] = int(float(value[idx]))
                                except (ValueError, TypeError):
                                    self.log("Warn: Return bad int value {} from {} override using default {}".format(value[idx], arg, default))
                                    self.record_status("Warn: Return bad int value {} from arg override {}".format(value[idx], arg), had_errors=True)
                                    value[idx] = default
                            elif isinstance(org_value, bool) and isinstance(value[idx], str):
                                # Convert to Boolean
                                if value[idx].lower() in ["on", "true", "yes", "enabled", "enable", "connected"]:
                                    value[idx] = True
                                else:
                                    value[idx] = False
                            self.log("Note: API Overridden arg {} value {} index {}".format(arg, value, idx))
                if index:
                    if index < len(value):
                        value = value[index]
                    else:
                        self.log("Warn: Out of range index {} within item {} value {}".format(index, arg, value))
                        value = None
            elif overrides:
                for override in overrides:
                    override_index = override.get("index", 0)
                    if override_index is None:
                        override_index = 0
                    if override_index == index:
                        value = override.get("value", value)
                        self.log("Note: API Overridden arg {} value {}".format(arg, value))
                        break

        # Get From HA config (not for domain specific which are apps.yaml options only)
        if value is None and not domain:
            value, default = self.get_ha_config(arg, default)

        # Resolve locally if no HA config
        if value is None:
            if (arg not in self.args) and (default is not None) and (index is not None):
                # Allow default to apply to all indices if there is not config item set
                index = None
            if domain:
                value = self.args.get(domain, {}).get(arg, default)
            else:
                value = self.args.get(arg, default)
            value = self.resolve_arg(arg, value, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index)

        if isinstance(default, float):
            # Convert to float?
            try:
                value = float(value)
            except (ValueError, TypeError):
                self.log("Warn: Return bad float value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn: Return bad float value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, int) and not isinstance(default, bool):
            # Convert to int?
            try:
                value = int(float(value))
            except (ValueError, TypeError):
                self.log("Warn: Return bad int value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn: Return bad int value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, bool) and isinstance(value, str):
            # Convert to Boolean
            if value.lower() in ["on", "true", "yes", "enabled", "enable", "connected"]:
                value = True
            else:
                value = False
        elif isinstance(default, list):
            # Convert to list?
            if not isinstance(value, list):
                value = [value]

        return value

    async def select_event(self, event, data, kwargs):
        """
        Catch HA Input select updates

        Parameters:
        - event: The event triggered by the input select.
        - data: The data associated with the event.
        - kwargs: Additional keyword arguments.

        Returns:
        None

        Description:
        This method is used to handle Home Assistant input select updates.
        It extracts the necessary information from the data and performs different actions based on the selected option.
        The actions include calling update service, saving and restoring settings, performing manual selection, and exposing configuration.
        After performing the actions, it triggers an update by setting update_pending flag to True and plan_valid flag to False.
        """
        service_data = data.get("service_data", {})
        value = service_data.get("option", None)
        entities = service_data.get("entity_id", [])

        # Can be a string or an array
        if isinstance(entities, str):
            entities = [entities]

        for entity_id in entities:
            if "predbat_gecloud_" in entity_id:
                if self.ge_cloud_direct:
                    await self.ge_cloud_direct.select_event(entity_id, value)

        for item in self.CONFIG_ITEMS:
            if ("entity" in item) and (item["entity"] in entities):
                entity = item["entity"]
                self.log("select_event: {}, {} = {}".format(item["name"], entity, value))
                if item["name"] == "update":
                    self.log("Calling update service for {}".format(value))
                    await self.async_download_predbat_version(value)
                elif item["name"] == "saverestore":
                    if value == "save current":
                        await self.async_update_save_restore_list()
                        await self.async_save_settings_yaml()
                    elif value == "restore default":
                        await self.async_restore_settings_yaml(None)
                    else:
                        await self.async_restore_settings_yaml(value)
                elif item.get("manual"):
                    await self.async_manual_select(item["name"], value)
                elif item.get("api"):
                    await self.async_api_select(item["name"], value)
                else:
                    await self.async_expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    async def number_event(self, event, data, kwargs):
        """
        Catch HA Input number updates

        This method is called when there is an update to a Home Assistant input number entity.
        It extracts the value and entity ID from the event data and processes it accordingly.
        If the entity ID matches any of the entities specified in the CONFIG_ITEMS list,
        it logs the entity and value, exposes the configuration item, and updates the pending plan.

        Args:
            event (str): The event name.
            data (dict): The event data.
            kwargs (dict): Additional keyword arguments.

        Returns:
            None
        """
        service_data = data.get("service_data", {})
        value = service_data.get("value", None)
        entities = service_data.get("entity_id", [])

        # Can be a string or an array
        if isinstance(entities, str):
            entities = [entities]

        for entity_id in entities:
            if "predbat_gecloud_" in entity_id:
                if self.ge_cloud_direct:
                    await self.ge_cloud_direct.number_event(entity_id, value)

        for item in self.CONFIG_ITEMS:
            if ("entity" in item) and (item["entity"] in entities):
                entity = item["entity"]
                self.log("number_event: {} = {}".format(entity, value))
                await self.async_expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    async def watch_event(self, entity, attribute, old, new, kwargs):
        """
        Catch HA state changes for watched entities
        """
        self.log("Watched event: {} = {} will trigger re-plan".format(entity, new))
        self.update_pending = True
        self.plan_valid = False

    async def switch_event(self, event, data, kwargs):
        """
        Catch HA Switch toggle

        This method is called when a Home Assistant switch is toggled. It handles the logic for updating the state of the switch
        and triggering any necessary actions based on the switch state.

        Parameters:
        - event (str): The event triggered by the switch toggle.
        - data (dict): Additional data associated with the event.
        - kwargs (dict): Additional keyword arguments.

        Returns:
        - None

        """
        service = data.get("service", None)
        service_data = data.get("service_data", {})
        entities = service_data.get("entity_id", [])

        # Can be a string or an array
        if isinstance(entities, str):
            entities = [entities]

        for entity_id in entities:
            if "predbat_gecloud_" in entity_id:
                if self.ge_cloud_direct:
                    await self.ge_cloud_direct.switch_event(entity_id, service)

        for item in self.CONFIG_ITEMS:
            if ("entity" in item) and (item["entity"] in entities):
                value = item["value"]
                entity = item["entity"]

                if service == "turn_on":
                    value = True
                elif service == "turn_off":
                    value = False
                elif service == "toggle" and isinstance(value, bool):
                    value = not value

                self.log("switch_event: {} = {}".format(entity, value))
                await self.async_expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    def get_ha_config(self, name, default):
        """
        Get Home assistant config value, use default if not set

        Parameters:
        name (str): The name of the config value to retrieve.
        default: The default value to use if the config value is not set.

        Returns:
        value: The value of the config if it is set, otherwise the default value.
        default: The default value passed as an argument.
        """
        item = self.config_index.get(name)
        if item and item["name"] == name:
            enabled = self.user_config_item_enabled(item)
            if enabled:
                value = item.get("value", None)
            else:
                value = None
            if default is None:
                default = item.get("default", None)
            if value is None:
                value = default
            return value, default
        return None, default

    async def async_expose_config(self, name, value, quiet=True, event=False, force=False, in_progress=False):
        return await self.run_in_executor(self.expose_config, name, value, quiet, event, force, in_progress)

    def expose_config(self, name, value, quiet=True, event=False, force=False, in_progress=False, force_ha=False):
        """
        Share the config with HA
        """
        item = self.config_index.get(name, None)
        if item:
            enabled = self.user_config_item_enabled(item)
            if not enabled:
                item["value"] = None
            else:
                entity = item.get("entity")
                has_changed = ((item.get("value", None) is None) or (value != item.get("value", None))) or force
                if entity and (has_changed or force_ha):
                    if has_changed and item.get("reset_inverter", False):
                        self.inverter_needs_reset = True
                        self.log("Set reset inverter true due to reset_inverter on item {}".format(name))
                    if has_changed and item.get("reset_inverter_force", False):
                        self.inverter_needs_reset = True
                        self.log("Set reset inverter true due to reset_inverter_force on item {}".format(name))
                        if event:
                            self.inverter_needs_reset_force = name
                            self.log("Set reset inverter force true due to reset_inverter_force on item {}".format(name))
                    item["value"] = value
                    if item["type"] == "input_number":
                        """INPUT_NUMBER"""
                        icon = item.get("icon", "mdi:numeric")
                        unit = item["unit"]
                        unit = unit.replace("£", self.currency_symbols[0])
                        unit = unit.replace("p", self.currency_symbols[1])
                        self.set_state_wrapper(
                            entity_id=entity,
                            state=value,
                            attributes={
                                "friendly_name": item["friendly_name"],
                                "min": item["min"],
                                "max": item["max"],
                                "step": item["step"],
                                "unit_of_measurement": unit,
                                "icon": icon,
                            },
                        )
                    elif item["type"] == "switch":
                        """SWITCH"""
                        icon = item.get("icon", "mdi:light-switch")
                        self.set_state_wrapper(entity_id=entity, state=("on" if value else "off"), attributes={"friendly_name": item["friendly_name"], "icon": icon})
                    elif item["type"] == "select":
                        """SELECT"""
                        icon = item.get("icon", "mdi:format-list-bulleted")
                        if value is None:
                            value = item.get("default", "")
                        options = item["options"]
                        if value not in options:
                            options.append(value)
                        old_state = self.get_state_wrapper(entity_id=entity)
                        if old_state and old_state != value:
                            self.set_state_wrapper(entity_id=entity, state=old_state, attributes={"friendly_name": item["friendly_name"], "options": options, "icon": icon})
                        self.set_state_wrapper(entity_id=entity, state=value, attributes={"friendly_name": item["friendly_name"], "options": options, "icon": icon})
                    elif item["type"] == "update":
                        """UPDATE"""
                        summary = self.releases.get("latest_body", "")
                        latest = self.releases.get("latest", "check HACS")
                        state = "off"
                        if item["installed_version"] != latest:
                            state = "on"
                        self.set_state_wrapper(
                            entity_id=entity,
                            state=state,
                            attributes={
                                "friendly_name": item["friendly_name"],
                                "title": item["title"],
                                "in_progress": in_progress,
                                "auto_update": True,
                                "installed_version": item["installed_version"],
                                "latest_version": latest,
                                "entity_picture": item["entity_picture"],
                                "release_url": item["release_url"],
                                "release_summary": summary,
                                "skipped_version": None,
                                "supported_features": 1,
                            },
                        )

    def user_config_item_enabled(self, item):
        """
        Check if user config item is enable
        """
        enable = item.get("enable", None)
        if enable:
            citem = self.config_index.get(enable, None)
            if citem:
                enabled_value = citem.get("value", True)
                if not enabled_value:
                    return False
                else:
                    return True
            else:
                self.log("Warn: Badly formed CONFIG enable item {}, please raise a Github ticket".format(item["name"]))
        return True

    async def async_update_save_restore_list(self):
        return await self.run_in_executor(self.update_save_restore_list)

    def update_save_restore_list(self):
        """
        Update list of current Predbat settings
        """
        global PREDBAT_SAVE_RESTORE
        self.save_restore_dir = self.config_root + "/predbat_save"

        if not os.path.exists(self.save_restore_dir):
            os.mkdir(self.save_restore_dir)

        PREDBAT_SAVE_RESTORE = ["save current", "restore default"]
        for root, dirs, files in os.walk(self.save_restore_dir):
            for name in files:
                filepath = os.path.join(root, name)
                if filepath.endswith(".yaml") and not name.startswith("."):
                    PREDBAT_SAVE_RESTORE.append(name)
        item = self.config_index.get("saverestore", None)
        item["options"] = PREDBAT_SAVE_RESTORE
        self.expose_config("saverestore", None)

    async def async_restore_settings_yaml(self, filename):
        """
        Restore settings from YAML file
        """
        self.save_restore_dir = self.config_root + "/predbat_save"

        # Create full hierarchical version of filepath to write to the logfile
        filepath_p = self.config_root_p + "/predbat_save"

        if filename != "previous.yaml":
            await self.async_save_settings_yaml("previous.yaml")

        if not filename:
            self.log("Restore settings to default")
            for item in self.CONFIG_ITEMS:
                if (item["value"] != item.get("default", None)) and item.get("restore", True):
                    self.log("Restore setting: {} = {} (was {})".format(item["name"], item["default"], item["value"]))
                    await self.async_expose_config(item["name"], item["default"], event=True)
            await self.async_call_notify("Predbat settings restored from default")
        else:
            filepath = os.path.join(self.save_restore_dir, filename)
            if os.path.exists(filepath):
                filepath_p = filepath_p + "/" + filename

                self.log("Restore settings from {}".format(filepath_p))
                with open(filepath, "r") as file:
                    settings = yaml.safe_load(file)
                    for item in settings:
                        current = self.config_index.get(item["name"], None)
                        if current and (current["value"] != item["value"]) and current.get("restore", True):
                            self.log("Restore setting: {} = {} (was {})".format(item["name"], item["value"], current["value"]))
                            await self.async_expose_config(item["name"], item["value"], event=True)
                await self.async_call_notify("Predbat settings restored from {}".format(filename))
        await self.async_expose_config("saverestore", None)

    def load_current_config(self):
        """
        Load the current configuration from a json file
        """
        if self.ha_interface.db_primary:
            # No need to save/restore config from a file if we are using the database
            return

        filepath = self.config_root + "/predbat_config.json"
        if os.path.exists(filepath):
            with open(filepath, "r") as file:
                try:
                    settings = json.load(file)
                except json.JSONDecodeError:
                    self.log("Warn: Failed to load Predbat settings from {}".format(filepath))
                    return

                for name in settings:
                    current = self.config_index.get(name, None)
                    if not current:
                        for item in self.CONFIG_ITEMS:
                            if item.get("oldname", "") == name:
                                self.log("Restore setting from old name {} to new name {}".format(name, item["name"]))
                                current = item
                    if current:
                        item_value = settings[name]
                        if current.get("value", None) != item_value:
                            # self.log("Restore saved setting: {} = {} (was {})".format(name, item_value, current.get("value", None)))
                            current["value"] = item_value

    def save_current_config(self):
        """
        Saves the currently defined configuration to a json file
        """
        if self.ha_interface.db_primary:
            # No need to save/restore config from a file if we are using the database
            return

        filepath = self.config_root + "/predbat_config.json"

        # Create full hierarchical version of filepath to write to the logfile
        filepath_p = self.config_root_p + "/predbat_config.json"

        save_array = {}
        for item in self.CONFIG_ITEMS:
            if item.get("save", True):
                if item.get("value", None) is not None:
                    save_array[item["name"]] = item["value"]
        with open(filepath, "w") as file:
            json.dump(save_array, file)
        self.log("Saved current settings to {}".format(filepath_p))

    async def async_save_settings_yaml(self, filename=None):
        """
        Save current Predbat settings
        """
        self.save_restore_dir = self.config_root + "/predbat_save"
        filepath_p = self.config_root_p + "/predbat_save"

        if not filename:
            filename = self.now_utc.strftime("%y_%m_%d_%H_%M_%S")
            filename += ".yaml"
        filepath = os.path.join(self.save_restore_dir, filename)
        filepath_p = filepath_p + "/" + filename

        with open(filepath, "w") as file:
            yaml.dump(self.CONFIG_ITEMS, file)
        self.log("Saved Predbat settings to {}".format(filepath_p))
        await self.async_call_notify("Predbat settings saved to {}".format(filename))

    def read_debug_yaml(self, filename):
        """
        Read debug yaml - used for debugging scenarios not for the main code
        """
        debug = {}
        if os.path.exists(filename):
            with open(filename, "r") as file:
                debug = yaml.safe_load(file)
        else:
            self.log("Warn: Debug file {} not found".format(filename))
            return

        for key in debug:
            if key not in ["CONFIG_ITEMS", "inverters"]:
                self.__dict__[key] = copy.deepcopy(debug[key])
            if key == "inverters":
                new_inverters = []
                for inverter in debug[key]:
                    inverter_obj = copy.deepcopy(self.inverters[0])
                    for key in inverter:
                        inverter_obj.__dict__[key] = copy.deepcopy(inverter[key])
                    new_inverters.append(inverter_obj)
                self.inverters = new_inverters

        for item in debug["CONFIG_ITEMS"]:
            current = self.config_index.get(item["name"], None)
            if current:
                # print("Restore setting: {} = {} (was {})".format(item["name"], item["value"], current["value"]))
                if current.get("value", None) != item.get("value", None):
                    current["value"] = item["value"]
        self.log("Restored debug settings - minutes now {}".format(self.minutes_now))

    def create_debug_yaml(self, write_file=True):
        """
        Write out a debug info yaml
        """
        time_now = self.now_utc.strftime("%H_%M_%S")
        basename = "/debug/predbat_debug_{}.yaml".format(time_now)
        filename = self.config_root + basename
        # Create full hierarchical version of filepath to write to the logfile
        filename_p = self.config_root_p + basename

        os.makedirs(os.path.dirname(filename), exist_ok=True)
        debug = {}

        # Store all predbat member variables into debug
        for key in self.__dict__:
            if not key.startswith("__") and not callable(getattr(self, key)):
                if (key.startswith("db")) or ("_key" in key) or key in DEBUG_EXCLUDE_LIST:
                    pass
                else:
                    if key == "args":
                        # Remove keys from args
                        debug[key] = copy.deepcopy(self.__dict__[key])
                        for sub_key in debug[key]:
                            if "_key" in sub_key:
                                debug[key][sub_key] = "xxx"
                    else:
                        debug[key] = self.__dict__[key]
        inverters_debug = []
        for inverter in self.inverters:
            inverter_debug = {}
            for key in inverter.__dict__:
                if not key.startswith("__") and not callable(getattr(inverter, key)):
                    if key.startswith("base"):
                        pass
                    else:
                        inverter_debug[key] = inverter.__dict__[key]
            inverters_debug.append(inverter_debug)
        debug["inverters"] = inverters_debug

        debug["CONFIG_ITEMS"] = copy.deepcopy(self.CONFIG_ITEMS)

        if write_file:
            with open(filename, "w") as file:
                yaml.dump(debug, file)
            self.log("Wrote debug yaml to {}".format(filename_p))
        else:
            # Return the debug yaml as a string
            return yaml.dump(debug)

    def create_entity_list(self):
        """
        Create the standard entity list
        """

        text = ""
        text += "# Predbat Dashboard - {}\n".format(THIS_VERSION)
        text += "type: entities\n"
        text += "Title: Predbat\n"
        text += "entities:\n"
        enable_list = [None]
        for item in self.CONFIG_ITEMS:
            enable = item.get("enable", None)
            if enable and enable not in enable_list:
                enable_list.append(enable)

        for try_enable in enable_list:
            for item in self.CONFIG_ITEMS:
                entity = item.get("entity", None)
                enable = item.get("enable", None)

                if entity and enable == try_enable and self.user_config_item_enabled(item):
                    text += "  - entity: " + entity + "\n"

        for entity in self.dashboard_index:
            text += "  - entity: " + entity + "\n"

        # Find path
        basename = "/predbat_dashboard.yaml"
        filename = self.config_root + basename
        # Create full hierarchical version of filepath to write to the logfile
        filename_p = self.config_root_p + basename

        # Write
        han = open(filename, "w")
        if han:
            self.log("Creating predbat dashboard at {}".format(filename_p))
            han.write(text)
            han.close()
        else:
            self.log("Failed to write predbat dashboard to {}".format(filename_p))

    def load_previous_value_from_ha(self, entity):
        """
        Load HA value either from state or from history if there is any
        """
        ha_value = self.get_state_wrapper(entity)
        if ha_value is not None:
            return ha_value

        history = self.get_history_wrapper(entity_id=entity, required=False)
        if history:
            history = history[0]
            if history:
                ha_value = history[-1]["state"]
        return ha_value

    async def trigger_watch_list(self, entity_id, attribute, old, new):
        """
        Trigger a watch event for an entity
        """
        for entity in self.watch_list:
            if entity_id == entity:
                await self.watch_event(entity, attribute, old, new, None)

    async def trigger_callback(self, service_data):
        """
        Trigger a callback for a service via HA Interface
        """
        for item in self.EVENT_LISTEN_LIST:
            if item["domain"] == service_data.get("domain", "") and item["service"] == service_data.get("service", ""):
                # self.log("Trigger callback for {} {}".format(item["domain"], item["service"]))
                await item["callback"](item["service"], service_data, None)

    def define_service_list(self):
        self.SERVICE_REGISTER_LIST = [
            {"domain": "input_number", "service": "set_value"},
            {"domain": "input_number", "service": "increment"},
            {"domain": "input_number", "service": "decrement"},
            {"domain": "switch", "service": "turn_on"},
            {"domain": "switch", "service": "turn_off"},
            {"domain": "switch", "service": "toggle"},
            {"domain": "select", "service": "select_option"},
            {"domain": "select", "service": "select_first"},
            {"domain": "select", "service": "select_last"},
            {"domain": "select", "service": "select_next"},
            {"domain": "select", "service": "select_previous"},
        ]
        self.EVENT_LISTEN_LIST = [
            {"domain": "switch", "service": "turn_on", "callback": self.switch_event},
            {"domain": "switch", "service": "turn_off", "callback": self.switch_event},
            {"domain": "switch", "service": "toggle", "callback": self.switch_event},
            {"domain": "input_number", "service": "set_value", "callback": self.number_event},
            {"domain": "input_number", "service": "increment", "callback": self.number_event},
            {"domain": "input_number", "service": "decrement", "callback": self.number_event},
            {"domain": "number", "service": "set_value", "callback": self.number_event},
            {"domain": "number", "service": "increment", "callback": self.number_event},
            {"domain": "number", "service": "decrement", "callback": self.number_event},
            {"domain": "select", "service": "select_option", "callback": self.select_event},
            {"domain": "select", "service": "select_first", "callback": self.select_event},
            {"domain": "select", "service": "select_last", "callback": self.select_event},
            {"domain": "select", "service": "select_next", "callback": self.select_event},
            {"domain": "select", "service": "select_previous", "callback": self.select_event},
            {"domain": "update", "service": "install", "callback": self.update_event},
            {"domain": "update", "service": "skip", "callback": self.update_event},
        ]

    def load_user_config(self, quiet=True, register=False):
        """
        Load config from HA
        """

        self.config_index = {}
        self.log("Refreshing Predbat configuration")

        # New install, used to set default of expert mode
        new_install = True
        current_status = self.load_previous_value_from_ha(self.prefix + ".status")
        if current_status:
            new_install = False
        else:
            self.log("New install detected")

        # Build config index
        for item in self.CONFIG_ITEMS:
            name = item["name"]
            self.config_index[name] = item

            if name == "mode" and new_install:
                item["default"] = PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]

        # Load current config (if there is one)
        if register:
            self.log("Loading current config")
            self.load_current_config()

        # Find values and monitor config
        for item in self.CONFIG_ITEMS:
            name = item["name"]
            type = item["type"]
            enabled = self.user_config_item_enabled(item)

            entity = type + "." + self.prefix + "_" + name
            item["entity"] = entity
            ha_value = None

            if not enabled:
                item["value"] = None

                # Remove the state if the entity still exists
                ha_value = self.get_state_wrapper(entity)
                if ha_value is not None:
                    self.set_state_wrapper(entity_id=entity, state=ha_value, attributes={"friendly_name": "[Disabled] " + item["friendly_name"]})
                continue

            # Get from current state, if not from HA directly
            ha_value = item.get("value", None)
            if ha_value is None:
                ha_value = self.load_previous_value_from_ha(entity)

            # Update drop down menu
            if name == "update":
                if not ha_value:
                    # Construct this version information as it's not set correctly already
                    ha_value = THIS_VERSION + " Loading..."
                else:
                    # Leave current value until it's set during version discovery later
                    continue

            # Default?
            if ha_value is None:
                ha_value = item.get("default", None)

            # Switch convert to text
            if type == "switch" and isinstance(ha_value, str):
                if ha_value.lower() in ["on", "true", "enable"]:
                    ha_value = True
                else:
                    ha_value = False

            if type == "input_number" and ha_value is not None:
                try:
                    ha_value = float(ha_value)
                except (ValueError, TypeError):
                    ha_value = None

            if type == "update":
                ha_value = None

            # Push back into current state
            if ha_value is not None:
                if item.get("manual"):
                    self.manual_times(name, new_value=ha_value)
                elif item.get("api"):
                    self.api_select_update(name, new_value=ha_value)
                else:
                    self.expose_config(item["name"], ha_value, quiet=quiet, force_ha=True)

        # Update the last time we refreshed the config
        self.set_state_wrapper(entity_id=self.prefix + ".config_refresh", state=self.now_utc.strftime(TIME_FORMAT))

        # Register HA services
        if register:
            self.watch_list = self.get_arg("watch_list", [], indirect=False)
            self.log("Watch list {}".format(self.watch_list))

            if not self.ha_interface.websocket_active and not self.ha_interface.db_primary:
                # Registering HA events as Websocket is not active
                for item in self.SERVICE_REGISTER_LIST:
                    self.fire_event("service_registered", domain=item["domain"], service=item["service"])
                for item in self.EVENT_LISTEN_LIST:
                    self.listen_select_handle = self.listen_event(item["callback"], event="call_service", domain=item["domain"], service=item["service"])

                for entity in self.watch_list:
                    if entity and isinstance(entity, str) and ("." in entity):
                        self.listen_state(self.watch_event, entity_id=entity)

        # Save current config to file if it was pending
        self.save_current_config()

    def resolve_arg_re(self, arg, arg_value, state_keys):
        """
        Resolve argument regular expression on list or string
        """
        matched = True

        if isinstance(arg_value, list):
            new_list = []
            for item_value in arg_value:
                item_matched, item_value = self.resolve_arg_re(arg, item_value, state_keys)
                if not item_matched:
                    self.log("Warn: Regular argument {} expression {} failed to match - disabling this item".format(arg, item_value))
                    new_list.append(None)
                else:
                    new_list.append(item_value)
            arg_value = new_list
        elif isinstance(arg_value, dict):
            new_dict = {}
            for item_name in arg_value:
                item_value = arg_value[item_name]
                item_matched, item_value = self.resolve_arg_re(arg, item_value, state_keys)
                if not item_matched:
                    self.log("Warn: Regular argument {} expression {} failed to match - disabling this item".format(arg, item_value))
                    new_dict[item_name] = None
                else:
                    new_dict[item_name] = item_value
            arg_value = new_dict
        elif isinstance(arg_value, str) and arg_value.startswith("re:"):
            matched = False
            my_re = "^" + arg_value[3:] + "$"
            for key in state_keys:
                res = re.search(my_re, key)
                if res:
                    if len(res.groups()) > 0:
                        self.log("Regular expression argument {} matched {} with {}".format(arg, my_re, res.group(1)))
                        arg_value = res.group(1)
                        matched = True
                        break
                    else:
                        self.log("Regular expression argument {} Matched {} with {}".format(arg, my_re, res.group(0)))
                        arg_value = res.group(0)
                        matched = True
                        break
        return matched, arg_value

    def auto_config(self):
        """
        Auto configure
        match arguments with sensors
        """

        states = self.get_state_wrapper()
        state_keys = states.keys()
        disabled = []
        self.unmatched_args = {}

        if 0:
            predbat_keys = []
            for key in state_keys:
                if "predbat" in str(key):
                    predbat_keys.append(key)
            predbat_keys.sort()
            self.log("Keys:\n  - entity: {}".format("\n  - entity: ".join(predbat_keys)))

        # Find each arg re to match
        for arg in self.args:
            arg_value = self.args[arg]
            matched, arg_value = self.resolve_arg_re(arg, arg_value, state_keys)
            if not matched:
                self.log("Warn: Regular expression argument: {} unable to match {}, now will disable".format(arg, arg_value))
                disabled.append(arg)
            else:
                self.args[arg] = arg_value

        # Remove unmatched keys
        for key in disabled:
            self.unmatched_args[key] = self.args[key]
            del self.args[key]

    def split_command_index(self, command):
        """
        Get the index of a command
        """
        command_index = None
        if "(" in command:
            command = command.replace(")", "")
            command_split = command.split("(")
            if len(command_split) > 1:
                command = command_split[0]
                command_index = int(command_split[1])
        return command, command_index

    def get_manual_api(self, command_type):
        """
        Get the manual API command
        """
        apply_commands = []
        command_index = None
        for api_command in self.manual_api:
            command_split = api_command.split("?")
            if len(command_split) > 1:
                command = command_split[0]
                command, command_index = self.split_command_index(command)
                command_args = command_split[1].split("&")
                args_dict = {}
                args_dict["index"] = command_index
                value = {}
                for arg in command_args:
                    arg_split = arg.split("=")
                    if len(arg_split) > 1:
                        value[arg_split[0]] = arg_split[1]
                    else:
                        value[arg_split[0]] = True
                args_dict["value"] = value
                if command == command_type:
                    apply_commands.append(args_dict)
            else:
                command_split = api_command.split("=")
                if len(command_split) > 1:
                    command = command_split[0]
                    command, command_index = self.split_command_index(command)
                    command_arg = command_split[1]
                    args_dict = {}
                    args_dict["index"] = command_index
                    args_dict["value"] = command_arg

                    if command == command_type:
                        apply_commands.append(args_dict)

        return apply_commands

    async def async_manual_select(self, config_item, value):
        """
        Async wrapper for selection on manual times dropdown
        """
        return await self.run_in_executor(self.manual_select, config_item, value)

    async def async_api_select(self, config_item, value):
        """
        Async wrapper for selection on api dropdown
        """
        return await self.run_in_executor(self.api_select, config_item, value)

    def manual_select(self, config_item, value):
        """
        Selection on manual times dropdown
        """
        item = self.config_index.get(config_item)
        if not item:
            return
        if not value:
            # Ignore null selections
            return
        if value.startswith("+"):
            # Ignore selections which are just the current value
            return
        values = item.get("value", "")
        if not values:
            values = ""
        values = values.replace("+", "")
        values_list = []
        exclude_list = []
        if values:
            values_list = values.split(",")
        if value == "off":
            values_list = []
        elif "[" in value:
            value = value.replace("[", "")
            value = value.replace("]", "")
            if value in values_list:
                values_list.remove(value)
        else:
            if value not in values_list:
                values_list.append(value)
                exclude_list.append(value)
        item_value = ",".join(values_list)
        if item_value:
            item_value = "+" + item_value

        if not item_value:
            item_value = "off"
        self.manual_times(config_item, new_value=item_value)

        # Update other drop downs that may need this time excluding
        for item in self.CONFIG_ITEMS:
            if item["name"] != config_item and item.get("manual"):
                value = item.get("value", "")
                if value and value != "reset" and exclude_list:
                    self.manual_times(item["name"], exclude=exclude_list)

    def api_select(self, config_item, value):
        """
        Selection on manual times dropdown
        """
        item = self.config_index.get(config_item)
        if not item:
            return
        if not value:
            # Ignore null selections
            return
        if value.startswith("+"):
            # Ignore selections which are just the current value
            return
        values = item.get("value", "")
        if not values:
            values = ""
        values = values.replace("+", "")
        values_list = []
        if values:
            values_list = values.split(",")
        if value == "off":
            values_list = []
        elif "[" in value:
            value = value.replace("[", "")
            value = value.replace("]", "")
            if value in values_list:
                values_list.remove(value)
        else:
            if value not in values_list:
                values_list.append(value)
        item_value = ",".join(values_list)
        if item_value:
            item_value = "+" + item_value

        if not item_value:
            item_value = "off"
        self.api_select_update(config_item, new_value=item_value)

    def api_select_update(self, config_item, new_value=None):
        """
        Update API selector
        """
        time_overrides = []

        # Deconstruct the value into a list of minutes
        item = self.config_index.get(config_item)
        if new_value:
            values = new_value
        else:
            values = item.get("value", "")

        values = values.replace("+", "")
        values_list = []
        if values:
            values_list = values.split(",")

        for value in values_list:
            if value == "off":
                continue
            for prev in time_overrides[:]:
                if "=" in prev:
                    prev_no_eq = prev.split("=")[0]
                elif "?" in prev:
                    prev_no_eq = prev.split("?")[0]
                if "=" in value:
                    value_no_eq = value.split("=")[0]
                elif "?" in value:
                    value_no_eq = value.split("?")[0]
                if prev_no_eq == value_no_eq:
                    time_overrides.remove(prev)
            time_overrides.append(value)

        values = ",".join(time_overrides)
        if values:
            values = "+" + values

        # Create the new dropdown
        time_values = []
        for minute_str in time_overrides:
            minute_str = "[" + minute_str + "]"
            time_values.append(minute_str)

        if values not in time_values:
            time_values.append(values)
        time_values.append("off")
        item["options"] = time_values
        if not values:
            values = "off"
        self.expose_config(config_item, values, force=True)
        return time_overrides

    def manual_times(self, config_item, exclude=[], new_value=None):
        """
        Update manual times sensor
        """
        time_overrides = []
        minutes_now = int(self.minutes_now / 30) * 30
        manual_time_max = 18 * 60

        # Deconstruct the value into a list of minutes
        item = self.config_index.get(config_item)
        if new_value:
            values = new_value
        else:
            values = item.get("value", "")
        values = values.replace("+", "")
        values_list = []
        if values:
            values_list = values.split(",")
        for value in values_list:
            if value == "off":
                continue
            try:
                start_time = datetime.strptime(value, "%H:%M:%S")
            except (ValueError, TypeError):
                start_time = None
            if start_time:
                minutes = start_time.hour * 60 + start_time.minute
                if minutes < minutes_now:
                    minutes += 24 * 60
                if (minutes - minutes_now) < manual_time_max:
                    time_overrides.append(minutes)

        # Reconstruct the list in order based on minutes
        values_list = []
        for minute in time_overrides:
            minute_str = (self.midnight + timedelta(minutes=minute)).strftime("%H:%M:%S")
            if minute_str not in exclude:
                values_list.append(minute_str)
        values = ",".join(values_list)
        if values:
            values = "+" + values

        # Create the new dropdown
        time_values = []
        for minute in range(minutes_now, minutes_now + manual_time_max, 30):
            minute_str = (self.midnight + timedelta(minutes=minute)).strftime("%H:%M:%S")
            if minute in time_overrides:
                minute_str = "[" + minute_str + "]"
            time_values.append(minute_str)

        if values not in time_values:
            time_values.append(values)
        time_values.append("off")
        item["options"] = time_values
        if not values:
            values = "off"
        self.expose_config(config_item, values, force=True)

        if time_overrides:
            time_txt = []
            for minute in time_overrides:
                time_txt.append(self.time_abs_str(minute))
        return time_overrides

    async def update_event(self, event, data, kwargs):
        """
        Update event.

        This function is called when an update event is triggered. It handles the logic for updating the application.

        Parameters:
        - event (str): The name of the event that triggered the update.
        - data (dict): Additional data associated with the update event.
        - kwargs (dict): Additional keyword arguments passed to the update event.

        Returns:
        None
        """
        self.log("Update event {} {} {}".format(event, data, kwargs))
        if data and data.get("service", "") == "install":
            service_data = data.get("service_data", {})
            if service_data.get("entity_id", "") == "update.predbat_version":
                latest = self.releases.get("latest", None)
                if latest:
                    self.log("Requested install of latest version {}".format(latest))
                    await self.async_download_predbat_version(latest)
        elif data and data.get("service", "") == "skip":
            self.log("Requested to skip the update, this is not yet supported...")
