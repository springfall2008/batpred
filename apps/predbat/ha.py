# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
import os
import re
import time
import math
from datetime import datetime, timedelta
import asyncio
from aiohttp import web, ClientSession, WSMsgType
import json
import requests
import traceback
import sqlite3
import threading
from config import TIME_FORMAT_HA, TIMEOUT, TIME_FORMAT_SECONDS

from db_manager import DatabaseManager  # database functions have been refactored to DatabaseManager

TIME_FORMAT_DB = "%Y-%m-%dT%H:%M:%S.%f"


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


class HAInterface:
    """
    Direct interface to Home Assistant
    """

    def __init__(self, base):
        """
        Initialize the interface to Home Assistant.
        """
        self.ha_url = base.args.get("ha_url", "http://supervisor/core")
        self.ha_key = base.args.get("ha_key", os.environ.get("SUPERVISOR_TOKEN", None))
        self.db_enable = base.args.get("db_enable", False)
        self.db_days = base.args.get("db_days", 30)
        self.db_mirror_ha = base.args.get("db_mirror_ha", False)
        self.db_primary = base.args.get("db_primary", False)
        self.db_mirror_list = {}
        self.db = None
        self.db_cursor = None
        self.websocket_active = False
        self.api_errors = 0

        self.base = base
        self.log = base.log
        self.state_data = {}
        self.slug = None

        if not self.ha_key:
            if not (self.db_enable and self.db_primary):
                self.log("Error: ha_key or SUPERVISOR_TOKEN not found, you must set ha_url/ha_key in apps.yaml")
                raise ValueError
            else:
                self.log("Info: Using SQL Lite database as primary data source, no HA interface available")
        else:
            check = self.api_call("/api/services")
            if not check:
                self.log("Warn: Unable to connect directly to Home Assistant at {}, please check your configuration of ha_url/ha_key".format(self.ha_url))
                self.ha_key = None
                raise ValueError
            else:
                self.log("Info: Connected to Home Assistant at {}".format(self.ha_url))

                res = self.api_call("/addons/self/info", core=False)
                if res:
                    # get add-on slug name which is the actual directory name under /addon_configs that /config is mounted to
                    self.slug = res["data"]["slug"]
                    self.log("Info: Add-on slug is {}".format(self.slug))

                self.base.create_task(self.socketLoop())
                self.websocket_active = True
                self.log("Info: Web Socket task started")

        if self.db_enable:
            # Open the SQL Lite database called predbat.db using the DatabaseManager
            self.log("Info: Opening database")
            self.db_manager = DatabaseManager(self.base, self.db_days)
            self.log("Info: Clean data older than {} days".format(self.db_days))
            self.db_manager.cleanup_db()

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
                        if self.base.stop_thread:
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
            self.base.fatal_error = True

        return response

    async def socketLoop(self):
        """
        Web socket loop for HA interface
        """
        error_count = 0

        while True:
            if self.base.stop_thread or self.base.fatal_error:
                self.log("Info: Web socket stopping")
                break
            if self.base.hass_api_version >= 2 and error_count >= 10:
                self.log("Error: Web socket failed 10 times, stopping")
                self.fatal_error = True
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

                        async for message in websocket:
                            if self.base.stop_thread:
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

            if not self.base.stop_thread:
                self.log("Warn: Web Socket closed, will try to reconnect in 5 seconds - error count {}".format(error_count))
                await asyncio.sleep(5)

        if not self.base.stop_thread:
            self.log("Error: Web Socket failed to reconnect, stopping....")
            self.websocket_active = False
            raise Exception("Web Socket failed to reconnect")

    def get_state(self, entity_id=None, default=None, attribute=None, refresh=False):
        """
        Get state from cached HA data (or from AppDaemon if used)
        """
        if entity_id:
            self.db_mirror_list[entity_id.lower()] = True

        if not entity_id:
            return self.state_data
        elif entity_id.lower() in self.state_data:
            if refresh:
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
                self.db_manager.mirror_updates.append({"entity_id": entity_id, "state": state, "attributes": attributes, "timestamp": self.base.now_utc_real})

    def db_tick(self):
        """
        Update the database with any new data
        """
        if not self.db_enable:
            return

        self.db_manager.db_tick()

    def update_states_db(self):
        """
        Update the state data from the SQL Lite database
        """
        if not self.db_enable:
            return

        entities = self.db_manager.get_all_entities()
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

    def get_history(self, sensor, now, days=30, force_db=False):
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

        start = now - timedelta(days=days)
        end = now
        res = self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        return res

    async def set_state_external(self, entity_id, state, attributes={}):
        """
        Used for external changes to Predbat state data
        """
        new_value = state
        new_state = {"entity_id": entity_id, "state": state, "attributes": attributes}
        old_value = self.get_state(entity_id)
        old_state = self.state_data.get(entity_id.lower(), None)
        found = False

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

        if not found:
            # If its not found then its a sensor and we can set the state directly
            self.set_state(entity_id, state, attributes)

            # Only trigger on value change or you get too many updates
            if (old_value is None) or (new_value != old_value):
                await self.trigger_watch_list(entity_id, attributes, old_state, new_state)

    def set_state(self, entity_id, state, attributes={}):
        """
        Set the state of an entity in Home Assistant.
        """
        self.db_mirror_list[entity_id] = True

        if self.db_mirror_ha or self.db_primary:
            item = self.db_manager.set_state_db_later(entity_id, state, attributes)
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

    def api_call(self, endpoint, data_in=None, post=False, core=True):
        """
        Make an API call to Home Assistant.

        :param endpoint: The API endpoint to call.
        :param data_in: The data to send in the body of the request.
        :param post: True if this is a POST request, False for GET.
        :return: The response from the API.
        """
        if core:
            url = self.ha_url + endpoint
        else:
            url = self.ha_url.replace("/core", "") + endpoint

        headers = {
            "Authorization": "Bearer " + self.ha_key,
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
            self.log("Warn: Failed to decode response {} from {}".format(response, url))
            self.api_errors += 1
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: Timeout from {}".format(url))
            self.api_errors += 1
            data = None

        if self.api_errors >= 10:
            self.log("Error: Too many API errors, stopping")
            self.base.fatal_error = True
        return data
