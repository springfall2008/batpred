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
from config import CONFIG_ITEMS


class UI:
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

        for item in CONFIG_ITEMS:
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

        for item in CONFIG_ITEMS:
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

        for item in CONFIG_ITEMS:
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
            value = item.get("value", None)
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
            for item in CONFIG_ITEMS:
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
                        for item in CONFIG_ITEMS:
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
            return

        filepath = self.config_root + "/predbat_config.json"

        # Create full hierarchical version of filepath to write to the logfile
        filepath_p = self.config_root_p + "/predbat_config.json"

        save_array = {}
        for item in CONFIG_ITEMS:
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
            yaml.dump(CONFIG_ITEMS, file)
        self.log("Saved Predbat settings to {}".format(filepath_p))
        await self.async_call_notify("Predbat settings saved to {}".format(filename))

    def create_debug_yaml(self):
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
        debug["TIME"] = self.time_now_str()
        debug["THIS_VERSION"] = THIS_VERSION
        debug["CONFIG_ITEMS"] = CONFIG_ITEMS
        debug["args"] = self.args
        debug["charge_window_best"] = self.charge_window_best
        debug["charge_limit_best"] = self.charge_limit_best
        debug["export_window_best"] = self.export_window_best
        debug["export_limits_best"] = self.export_limits_best
        debug["low_rates"] = self.low_rates
        debug["high_export_rates"] = self.high_export_rates
        debug["load_forecast"] = self.load_forecast
        debug["load_minutes_step"] = self.load_minutes_step
        debug["load_minutes_step10"] = self.load_minutes_step10
        debug["pv_forecast_minute_step"] = self.pv_forecast_minute_step
        debug["pv_forecast_minute10_step"] = self.pv_forecast_minute10_step
        debug["yesterday_load_step"] = self.yesterday_load_step
        debug["yesterday_pv_step"] = self.yesterday_pv_step

        with open(filename, "w") as file:
            yaml.dump(debug, file)
        self.log("Wrote debug yaml to {}".format(filename_p))

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
        for item in CONFIG_ITEMS:
            enable = item.get("enable", None)
            if enable and enable not in enable_list:
                enable_list.append(enable)

        for try_enable in enable_list:
            for item in CONFIG_ITEMS:
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
                # print("Trigger callback for {} {}".format(item["domain"], item["service"]))
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
        for item in CONFIG_ITEMS:
            name = item["name"]
            self.config_index[name] = item

            if name == "mode" and new_install:
                item["default"] = PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]

        # Load current config (if there is one)
        if register:
            self.log("Loading current config")
            self.load_current_config()

        # Find values and monitor config
        for item in CONFIG_ITEMS:
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

            if not self.ha_interface.websocket_active:
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
