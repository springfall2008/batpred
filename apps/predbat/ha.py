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
from datetime import timedelta, datetime, timezone
import asyncio
from aiohttp import ClientSession, WSMsgType
import json
import requests
import traceback
import threading
import time
from utils import str2time
from config import TIME_FORMAT_HA, TIMEOUT, TIME_FORMAT_HA_TZ
from component_base import ComponentBase


class RunThread(threading.Thread):
    def __init__(self, coro):
        self.coro = coro
        self.result = None
        super().__init__()

    def run(self):
        self.result = asyncio.run(self.coro)


def run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        thread = RunThread(coro)
        thread.start()
        thread.join()
        return thread.result
    else:
        return asyncio.run(coro)


class HAHistory(ComponentBase):
    """
    Home Assistant History Data
    """

    def initialize(self):
        self.history_entities = {}
        self.history_data = {}

    def add_entity(self, entity_id, days):
        """
        Add an entity to the history list
        """
        if entity_id in self.history_entities:
            if days > self.history_entities[entity_id]:
                self.history_entities[entity_id] = days
        else:
            self.history_entities[entity_id] = days

    def get_history(self, entity_id, days=30, tracked=True):
        """
        Get history data for an entity
        """
        if self.history_data.get(entity_id, None) and self.history_entities.get(entity_id, 0) >= days:
            return [self.history_data[entity_id]]
        else:
            ha_interface = self.base.components.get_component("ha")
            if not ha_interface:
                self.log("Error: HAHistory: No HAInterface available, cannot fetch history")
                return None
            if tracked:
                self.add_entity(entity_id, days)
                days = self.history_entities[entity_id]
            history_data = ha_interface.get_history(entity_id, datetime.now(self.local_tz), days=days)
            if history_data and len(history_data) > 0:
                history_data = history_data[0]
                if tracked:
                    self.update_entity(entity_id, history_data)
                return [history_data]
        return None

    def prune_history(self, now):
        """
        Prune history data older than required
        """
        for entity_id in list(self.history_data.keys()):
            max_days = self.history_entities.get(entity_id, 30)
            cutoff_time = now - timedelta(days=max_days)
            new_history = []
            keep_all = False
            for entry in self.history_data[entity_id]:
                if keep_all:
                    new_history.append(entry)
                else:
                    last_updated = entry.get("last_updated", None)
                    if last_updated:
                        entry_time = str2time(last_updated)
                        if entry_time >= cutoff_time:
                            new_history.append(entry)
                            # Keep remaining entries now as they are in order
                            keep_all = True
            self.history_data[entity_id] = new_history

    def update_entity(self, entity_id, new_history_data):
        """
        Update history data for an entity
        """
        if not new_history_data:
            return

        FILTER_ATTRIBUTES = ["friendly_name", "unit_of_measurement", "icon", "device_class", "results", "state_class", "entity_id"]
        FILTER_ENTRIES = ["last_changed", "entity_id"]

        # Filter useless data from history data
        for entry in new_history_data:
            if entry.get("attributes"):
                for attr in FILTER_ATTRIBUTES:
                    entry["attributes"].pop(attr, None)
            for entry_attr in FILTER_ENTRIES:
                entry.pop(entry_attr, None)

        current_history_data = self.history_data.get(entity_id, None)
        last_updated = current_history_data[-1].get("last_updated", None) if current_history_data and len(current_history_data) > 0 else None
        count_added = 0
        if last_updated:
            # Find the last timestamp in the previous history data, data is always in order from oldest to newest
            last_timestamp = str2time(last_updated)
            # Scan new data, using the timestamp only add new entries
            add_all = False
            for entry in new_history_data:
                if add_all:
                    self.history_data[entity_id].append(entry)
                    count_added += 1
                else:
                    this_updated = entry.get("last_updated", None)
                    if this_updated:
                        entry_time = str2time(this_updated)
                        if entry_time > last_timestamp:
                            self.history_data[entity_id].append(entry)
                            add_all = True  # Remaining entries are all newer
                            count_added += 1
        else:
            count_added += len(new_history_data)
            self.history_data[entity_id] = new_history_data

        # Update last success timestamp
        self.update_success_timestamp()

    async def run(self, seconds, first):
        if first:
            self.log("Info: Starting HAHistory")
            ha_interface = self.base.components.get_component("ha")
            if not ha_interface:
                self.log("Error: HAHistory: No HAInterface available, cannot start history updates")
                return False

        ha_interface = self.base.components.get_component("ha")
        if seconds % (2 * 60) == 0:
            # Update history data every 2 minutes
            now = datetime.now(self.local_tz)
            for entity_id in list(self.history_entities.keys()):
                # self.log("HAHistory: Updating history for {}".format(entity_id))
                current_history_data = self.history_data.get(entity_id, None)
                last_updated = current_history_data[-1].get("last_updated", None) if current_history_data and len(current_history_data) > 0 else None
                if last_updated:
                    history_data = ha_interface.get_history(entity_id, now, days=1, from_time=str2time(last_updated))
                    if history_data and len(history_data) > 0:
                        history_data = history_data[0]
                        self.update_entity(entity_id, history_data)
                else:
                    history_data = ha_interface.get_history(entity_id, now, days=self.history_entities[entity_id])
                    if history_data and len(history_data) > 0:
                        history_data = history_data[0]
                        self.update_entity(entity_id, history_data)

        if seconds % (60 * 60) == 0:
            # Prune history data every hour
            self.log("Info: HAHistory: Pruning history data")
            self.prune_history(datetime.now(self.local_tz))
        return True


class HAInterface(ComponentBase):
    """
    Direct interface to Home Assistant
    """

    def is_alive(self):
        if not self.api_started:
            return False
        if self.ha_key and not self.websocket_active:
            return False
        return True

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("HAInterface: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: HAInterface: Failed to start")
            return False
        return True

    def initialize(self, ha_url, ha_key, db_enable, db_mirror_ha, db_primary):
        """
        Initialize the interface to Home Assistant.
        """
        self.ha_url = ha_url
        self.ha_key = ha_key
        self.db_enable = db_enable
        self.db_mirror_ha = db_mirror_ha
        self.db_primary = db_primary
        self.db_manager = None
        self.db_mirror_list = {}
        self.db = None
        self.db_cursor = None
        self.websocket_active = False
        self.api_errors = 0
        self.last_success_timestamp = None

        self.state_data = {}
        self.slug = None

        if not self.ha_key:
            if not (self.db_enable and self.db_primary):
                self.log("Error: ha_key or SUPERVISOR_TOKEN not found, you must set ha_url/ha_key in apps.yaml")
                raise ValueError
            else:
                self.log("Info: Using SQL Lite database as primary data source, no HA interface available")

        if self.ha_key:
            # Get the current addon info, but suppress warning message if the API call fails as non-HAOS installs won't have supervisor running
            res = self.api_call("/addons/self/info", core=False, silent=True)
            if res:
                # get add-on slug name which is the actual directory name under /addon_configs that /config is mounted to
                self.slug = res["data"]["slug"]
                self.log("Info: Add-on slug is {}".format(self.slug))

            check = self.api_call("/api/services")
            if not check:
                self.log("Warn: Unable to connect directly to Home Assistant at {}, please check your configuration of ha_url/ha_key".format(self.ha_url))
                self.ha_key = None
                raise ValueError

        if self.db_enable:
            # Open the SQL Lite database called predbat.db using the DatabaseManager
            self.db_manager = self.base.components.get_component("db")

        # API Has started
        self.base.ha_interface = self  # Set pointer back to ourselves as other components require this one

    async def start(self):
        if self.ha_key:
            self.log("Info: Starting HA interface")
            self.websocket_active = True
            await self.socketLoop()
        else:
            self.log("Info: Starting Dummy HA interface")
            seconds = 0
            self.api_started = True
            while not self.api_stop:
                if seconds % 60 == 0:
                    self.last_success_timestamp = datetime.now(timezone.utc)
                await asyncio.sleep(5)
                seconds += 5
        self.api_started = False

    def get_slug(self):
        """
        Get the add-on slug.
        """
        return self.slug

    def call_service_websocket_command(self, domain, service, data):
        """
        Call a service via the web socket interface
        """
        return run_async(self.async_call_service_websocket_command(domain, service, data))

    async def async_call_service_websocket_command(self, domain, service, service_data):
        """
        Call a service via the web socket interface
        """
        url = "{}/api/websocket".format(self.ha_url)
        response = None
        # self.log("Info: Web socket service {}/{} socket for url {}".format(domain, service, url))

        return_response = service_data.get("return_response", False)
        if "return_response" in service_data:
            del service_data["return_response"]

        async with ClientSession() as session:
            try:
                async with session.ws_connect(url) as websocket:
                    await websocket.send_json({"type": "auth", "access_token": self.ha_key})
                    id = 1
                    await websocket.send_json({"id": id, "type": "call_service", "domain": domain, "service": service, "service_data": service_data, "return_response": return_response})

                    async for message in websocket:
                        if self.api_stop:
                            self.log("Info: Web socket stopping")
                            break

                        if message.type == WSMsgType.TEXT:
                            try:
                                data = json.loads(message.data)
                                if data:
                                    message_type = data.get("type", "")
                                    if message_type == "result":
                                        response = data.get("result", {}).get("response", None)
                                        success = data.get("success", False)
                                        self.api_errors = 0

                                        if not success:
                                            self.log("Warn: Service call {}/{} data {} failed with response {}".format(domain, service, service_data, response))
                                        break

                            except Exception as e:
                                self.log("Error: Web Socket exception in update loop: {}".format(e))
                                self.log("Error: " + traceback.format_exc())
                                self.api_errors += 1
                                break

            except Exception as e:
                self.log("Error: Web Socket exception in startup: {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.api_errors += 1

        if self.api_errors >= 10:
            self.log("Error: Too many API errors, stopping")
            self.fatal_error_occurred()

        return response

    async def socketLoop(self):
        """
        Web socket loop for HA interface
        """
        error_count = 0

        while True:
            if self.api_stop or self.fatal_error:
                self.log("Info: Web socket stopping")
                break
            if error_count >= 10:
                self.log("Error: Web socket failed 10 times, stopping")
                self.fatal_error_occurred()
                break

            url = "{}/api/websocket".format(self.ha_url)
            self.log("Info: Start socket for url {}".format(url))
            async with ClientSession() as session:
                try:
                    async with session.ws_connect(url) as websocket:
                        await websocket.send_json({"type": "auth", "access_token": self.ha_key})
                        sid = 1

                        # Connected okay, reset error count
                        error_count = 0

                        # Subscribe to all state changes
                        await websocket.send_json({"id": sid, "type": "subscribe_events", "event_type": "state_changed"})
                        sid += 1

                        # Listen for services
                        await websocket.send_json({"id": sid, "type": "subscribe_events", "event_type": "call_service"})
                        sid += 1

                        # Get service list
                        # await websocket.send_json({"id": sid, "type": "get_services"})
                        # sid += 1

                        # Fire events to say we have registered services
                        for item in self.base.SERVICE_REGISTER_LIST:
                            await websocket.send_json({"id": sid, "type": "fire_event", "event_type": "service_registered", "event_data": {"service": item["service"], "domain": item["domain"]}})
                            sid += 1

                        self.log("Info: Web Socket active")
                        self.base.update_pending = True  # Force an update when web-socket reconnects
                        self.api_started = True

                        async for message in websocket:
                            if self.api_stop or self.fatal_error:
                                self.log("Info: Web socket stopping")
                                break

                            if message.type == WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                    if data:
                                        message_type = data.get("type", "")
                                        if message_type == "event":
                                            event_info = data.get("event", {})
                                            event_type = event_info.get("event_type", "")
                                            if event_type == "state_changed":
                                                event_data = event_info.get("data", {})
                                                old_state = event_data.get("old_state", {})
                                                new_state = event_data.get("new_state", {})
                                                if new_state:
                                                    entity_id = new_state.get("entity_id", None)
                                                    if entity_id:
                                                        self.update_state_item(new_state, entity_id)
                                                    else:
                                                        self.log("Warn: Web Socket state_changed event has no entity_id {}".format(new_state))
                                                    # Only trigger on value change or you get too many updates
                                                    if not old_state or (new_state.get("state", None) != old_state.get("state", None)):
                                                        await self.base.trigger_watch_list(new_state["entity_id"], event_data.get("attribute", None), event_data.get("old_state", None), new_state)
                                            elif event_type == "call_service":
                                                service_data = event_info.get("data", {})
                                                await self.base.trigger_callback(service_data)
                                            else:
                                                self.log("Info: Web Socket unknown message {}".format(data))
                                        elif message_type == "result":
                                            success = data.get("success", False)
                                            if not success:
                                                self.log("Warn: Web Socket result failed {}".format(data))
                                            # result = data.get("result", {})
                                            # resultid = data.get("id", None)
                                            # if result:
                                            #    self.log("Info: Web Socket result id {} data {}".format(resultid, result))
                                        elif message_type == "auth_required":
                                            pass
                                        elif message_type == "auth_ok":
                                            pass
                                        elif message_type == "auth_invalid":
                                            self.log("Warn: Web Socket auth failed, check your ha_key setting")
                                            self.websocket_active = False
                                            raise Exception("Web Socket auth failed")
                                        else:
                                            self.log("Info: Web Socket unknown message {}".format(data))

                                        self.update_success_timestamp()

                                except Exception as e:
                                    self.log("Error: Web Socket exception in update loop: {}".format(e))
                                    self.log("Error: " + traceback.format_exc())
                                    error_count += 1
                                    break

                            elif message.type == WSMsgType.CLOSED:
                                error_count += 1
                                break
                            elif message.type == WSMsgType.ERROR:
                                error_count += 1
                                break

                except Exception as e:
                    self.log("Error: Web Socket exception in startup: {}".format(e))
                    self.log("Error: " + traceback.format_exc())
                    error_count += 1

            if not self.api_stop:
                self.log("Warn: Web Socket closed, will try to reconnect in 5 seconds - error count {}".format(error_count))
                await asyncio.sleep(5)

        self.api_started = False
        if not self.api_stop:
            self.log("Error: Web Socket failed to reconnect, stopping....")
            self.websocket_active = False
            raise Exception("Web Socket failed to reconnect")

    def get_state(self, entity_id=None, default=None, attribute=None, refresh=False):
        """
        Get state from cached HA data
        """
        if entity_id:
            self.db_mirror_list[entity_id.lower()] = True

        if not entity_id:
            return self.state_data
        elif entity_id.lower() in self.state_data:
            if refresh and not self.ha_key:
                # Only refresh from DB/HA if we are not the primary data source
                self.update_state(entity_id)
            state_info = self.state_data[entity_id.lower()]
            if attribute:
                if attribute in state_info["attributes"]:
                    return state_info["attributes"][attribute]
                else:
                    return default
            else:
                return state_info["state"]
        else:
            return default

    def update_state_db(self, entity_id):
        """
        Update state for entity_id from the SQLLite database
        """
        self.db_mirror_list[entity_id.lower()] = True
        item = self.db_manager.get_state_db(entity_id)
        if item:
            self.update_state_item(item, entity_id, nodb=True)

    def update_state(self, entity_id):
        """
        Update state for entity_id
        """
        self.db_mirror_list[entity_id.lower()] = True

        if self.db_primary and self.db_enable and not self.ha_key:
            self.update_state_db(entity_id)
            return

        if not self.ha_key:
            return

        item = self.api_call("/api/states/{}".format(entity_id))
        if item:
            self.update_state_item(item, entity_id)

    def update_state_item(self, item, entity_id, nodb=False):
        """
        Update state table for item
        """
        entity_id = entity_id.lower()
        attributes = item.get("attributes", {})
        last_changed = item.get("last_changed", item.get("last_updated", None))
        if "state" in item:
            state = item["state"]
            self.state_data[entity_id] = {"state": state, "attributes": attributes, "last_changed": last_changed}
            if not nodb and ((self.db_mirror_ha and (entity_id in self.db_mirror_list)) or self.db_primary):
                # Instead of appending to a local mirror_updates list, call the database manager to schedule the update
                if last_changed:
                    try:
                        last_changed = datetime.strptime(last_changed, TIME_FORMAT_HA_TZ)
                    except (ValueError, TypeError) as e:
                        self.log("Warn: Failed to parse last_changed time {} for entity {} : {}".format(last_changed, entity_id, e))
                        last_changed = datetime.now()

                self.db_manager.set_state_db(entity_id, state, attributes, timestamp=last_changed)

    def update_states_db(self):
        """
        Update the state data from the SQL Lite database
        """
        if not self.db_enable:
            return

        entities = self.db_manager.get_all_entities_db()
        for entity_name in entities:
            self.update_state_db(entity_name)

    def update_states(self):
        """
        Update the state data from Home Assistant.
        """

        if self.db_primary and self.db_enable and not self.ha_key:
            self.update_states_db()
            return

        if not self.ha_key:
            return

        res = self.api_call("/api/states")
        if res:
            self.state_data = {}
            for item in res:
                entity_id = item.get("entity_id", None)
                if entity_id:
                    self.update_state_item(item, entity_id)
                else:
                    self.log("Warn: Failed to update state data from HA, item has no entity_id {}".format(item))
        else:
            self.log("Warn: Failed to update state data from HA")

    def get_history(self, sensor, now, days=30, from_time=None, force_db=False):
        """
        Get the history for a sensor from Home Assistant.

        :param sensor: The sensor to get the history for.
        :return: The history for the sensor.
        """
        if not sensor:
            return None

        self.db_mirror_list[sensor.lower()] = True

        if (self.db_primary or force_db) and self.db_enable:
            return self.db_manager.get_history_db(sensor, now, days=days)

        if from_time:
            start = from_time
        else:
            start = now - timedelta(days=days)
        end = now
        res = self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        if isinstance(res, list) and len(res) > 0:
            return res
        else:
            return None

    async def set_state_external(self, entity_id, state, attributes={}):
        """
        Used for external changes to Predbat state data
        """
        new_value = state
        new_state = {"entity_id": entity_id, "state": state, "attributes": attributes}
        old_value = self.get_state(entity_id)
        old_state = self.state_data.get(entity_id.lower(), None)

        # See if the modified entity is in the CONFIG_ITEMS list
        for item in self.base.CONFIG_ITEMS:
            if item.get("entity") == entity_id:
                old_value = item.get("value", "")
                step = item.get("step", 1)
                itemtype = item.get("type", "")
                if old_value is None:
                    old_value = item.get("default", "")
                if itemtype == "input_number" and step == 1:
                    old_value = int(old_value)
                if old_value != new_value:
                    service_data = {}
                    service_data["domain"] = itemtype
                    if itemtype == "switch":
                        service_data["service"] = "turn_on" if new_value else "turn_off"
                        service_data["service_data"] = {"entity_id": entity_id}
                    elif itemtype == "input_number":
                        service_data["service"] = "set_value"
                        service_data["service_data"] = {"entity_id": entity_id, "value": new_value}
                    elif itemtype == "select":
                        service_data["service"] = "select_option"
                        service_data["service_data"] = {"entity_id": entity_id, "option": new_value}
                    else:
                        continue

                    await self.base.trigger_callback(service_data)
                    return

        # Call back to Predbat handled entities
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        service_data = {"domain": domain}
        if domain in ["switch", "input_boolean"]:
            # For toggle switches, check if value is 'on' or 'off'
            service_data["service"] = "turn_on" if new_value else "turn_off"
            service_data["service_data"] = {"entity_id": entity_id}
        elif domain in ["number", "input_number"]:
            service_data["service"] = "set_value"
            service_data["service_data"] = {"entity_id": entity_id, "value": new_value}
        elif domain in ["select", "input_select"]:
            service_data["service"] = "select_option"
            service_data["service_data"] = {"entity_id": entity_id, "option": new_value}
        else:
            service_data = None
        if service_data:
            await self.base.trigger_callback(service_data)
            return

        # If its not found then its a sensor and we can set the state directly
        self.set_state(entity_id, state, attributes)

        # Only trigger on value change or you get too many updates
        if (old_value is None) or (new_value != old_value):
            await self.base.trigger_watch_list(entity_id, attributes, old_state, new_state)

    def set_state(self, entity_id, state, attributes={}):
        """
        Set the state of an entity in Home Assistant.
        """
        self.db_mirror_list[entity_id] = True

        if self.db_mirror_ha or self.db_primary:
            item = self.db_manager.set_state_db(entity_id, state, attributes)
            # Locally cache state until DB update happens
            self.update_state_item(item, entity_id, nodb=True)

        if self.ha_key:
            data = {"state": state}
            if attributes:
                data["attributes"] = attributes
                data["unrecorded_attributes"] = ["results"]
            self.api_call("/api/states/{}".format(entity_id), data, post=True)
            self.update_state(entity_id)

    def call_service(self, service, **kwargs):
        """
        Call a service in Home Assistant via Websocket
        or loopback to Predbat in standalone mode
        """
        data = {}
        for key in kwargs:
            data[key] = kwargs[key]
        domain, service = service.split("/")
        if self.websocket_active:
            return self.call_service_websocket_command(domain, service, data)
        else:
            # Loopback with no home assistant
            data_frame = {"domain": domain, "service": service, "service_data": data}
            return run_async(self.base.trigger_callback(data_frame))

    def api_call(self, endpoint, data_in=None, post=False, core=True, silent=False):
        """
        Make an API call to Home Assistant.

        :param endpoint: The API endpoint to call.
        :param data_in: The data to send in the body of the request.
        :param post: True if this is a POST request, False for GET.
        :param core: True is this is a call to HA Core, False if it is a Supervisor call
        :param silent: True if warning message from the API call is to be suppressed
        :return: The response from the API.
        """
        if core:
            url = self.ha_url + endpoint
            key = self.ha_key
        else:
            # make an API call to the Supervisor using the supervisor token
            url = "http://supervisor" + endpoint
            key = os.environ.get("SUPERVISOR_TOKEN", None)

        if key is None:
            return None

        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if post:
            if data_in:
                response = requests.post(url, headers=headers, json=data_in, timeout=TIMEOUT)
            else:
                response = requests.post(url, headers=headers, timeout=TIMEOUT)
        else:
            if data_in:
                response = requests.get(url, headers=headers, params=data_in, timeout=TIMEOUT)
            else:
                response = requests.get(url, headers=headers, timeout=TIMEOUT)
        try:
            data = response.json()
            self.api_errors = 0
        except requests.exceptions.JSONDecodeError:
            if not silent:  # suppress warning message for call to get slug id from supervisor because in docker installs this will always error (no supervisor)
                self.log("Warn: Failed to decode response {} from {}".format(response, url))
                self.api_errors += 1

            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: Timeout from {}".format(url))
            self.api_errors += 1
            data = None

        if self.api_errors >= 10:
            self.log("Error: Too many API errors, stopping")
            self.fatal_error_occurred()

        return data
