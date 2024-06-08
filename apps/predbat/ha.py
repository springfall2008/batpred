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
from config import TIME_FORMAT_HA, TIMEOUT


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
        self.websocket_active = False

        self.base = base
        self.log = base.log
        self.state_data = {}
        if not self.ha_key:
            self.log("Warn: ha_key or SUPERVISOR_TOKEN not found, you can set ha_url/ha_key in apps.yaml. Will use direct HA API")
        else:
            check = self.api_call("/api/")
            if not check:
                self.log("Warn: Unable to connect directly to Home Assistant at {}, please check your configuration of ha_url/ha_key".format(self.ha_url))
                self.ha_key = None
            else:
                self.log("Info: Connected to Home Assistant at {}".format(self.ha_url))
                self.base.create_task(self.socketLoop())
                self.websocket_active = True
                self.log("Info: Web Socket task started")

    async def socketLoop(self):
        """
        Web socket loop for HA interface
        """
        while True:
            if self.base.stop_thread:
                self.log("Info: Web socket stopping")
                break

            url = "{}/api/websocket".format(self.ha_url)
            self.log("Info: Start socket for url {}".format(url))
            async with ClientSession() as session:
                try:
                    async with session.ws_connect(url) as websocket:
                        await websocket.send_json({"type": "auth", "access_token": self.ha_key})
                        sid = 1

                        # Subscribe to all state changes
                        await websocket.send_json({"id": sid, "type": "subscribe_events", "event_type": "state_changed"})
                        sid += 1

                        # Listen for services
                        await websocket.send_json({"id": sid, "type": "subscribe_events", "event_type": "call_service"})
                        sid += 1

                        # Fire events to say we have registered services
                        for item in self.base.SERVICE_REGISTER_LIST:
                            await websocket.send_json(
                                {"id": sid, "type": "fire_event", "event_type": "service_registered", "event_data": {"service": item["service"], "domain": item["domain"]}}
                            )
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
                                                    self.update_state_item(new_state)
                                                    # Only trigger on value change or you get too many updates
                                                    if not old_state or (new_state.get("state", None) != old_state.get("state", None)):
                                                        await self.base.trigger_watch_list(
                                                            new_state["entity_id"], event_data.get("attribute", None), event_data.get("old_state", None), new_state
                                                        )
                                            elif event_type == "call_service":
                                                service_data = event_info.get("data", {})
                                                await self.base.trigger_callback(service_data)
                                            else:
                                                self.log("Info: Web Socket unknown message {}".format(data))
                                        elif message_type == "result":
                                            success = data.get("success", False)
                                            if not success:
                                                self.log("Warn: Web Socket result failed {}".format(data))
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
                                    break

                            elif message.type == WSMsgType.CLOSED:
                                break
                            elif message.type == WSMsgType.ERROR:
                                break

                except Exception as e:
                    self.log("Error: Web Socket exception in startup: {}".format(e))
                    self.log("Error: " + traceback.format_exc())

            if not self.base.stop_thread:
                self.log("Warn: Web Socket closed, will try to reconnect in 5 seconds")
                await asyncio.sleep(5)

    def get_state(self, entity_id=None, default=None, attribute=None, refresh=False):
        """
        Get state from cached HA data (or from appDaemon if used)
        """
        if not self.ha_key:
            return self.base.get_state(entity_id=entity_id, default=default, attribute=attribute)

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

    def update_state(self, entity_id):
        """
        Update state for entity_id
        """
        if not self.ha_key:
            return
        item = self.api_call("/api/states/{}".format(entity_id))
        if item:
            self.update_state_item(item)

    def update_state_item(self, item):
        """
        Update state table for item
        """
        entity_id = item["entity_id"]
        attributes = item["attributes"]
        last_changed = item["last_changed"]
        state = item["state"]
        self.state_data[entity_id.lower()] = {"state": state, "attributes": attributes, "last_changed": last_changed}

    def update_states(self):
        """
        Update the state data from Home Assistant.
        """
        if not self.ha_key:
            return
        res = self.api_call("/api/states")
        if res:
            self.state_data = {}
            for item in res:
                self.update_state_item(item)
        else:
            self.log("Warn: Failed to update state data from HA")

    def get_history(self, sensor, now, days=30):
        """
        Get the history for a sensor from Home Assistant.

        :param sensor: The sensor to get the history for.
        :return: The history for the sensor.
        """
        if not self.ha_key:
            return self.base.get_history_ad(sensor, days=days)

        start = now - timedelta(days=days)
        end = now
        res = self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        return res

    def set_state(self, entity_id, state, attributes={}):
        """
        Set the state of an entity in Home Assistant.
        """
        if not self.ha_key:
            if attributes:
                return self.base.set_state(entity_id, state=state, attributes=attributes)
            else:
                return self.base.set_state(entity_id, state=state)

        data = {"state": state}
        if attributes:
            data["attributes"] = attributes
        self.api_call("/api/states/{}".format(entity_id), data, post=True)
        self.update_state(entity_id)

    def call_service(self, service, **kwargs):
        """
        Call a service in Home Assistant.
        """
        if not self.ha_key:
            return self.base.call_service(service, **kwargs)

        data = {}
        for key in kwargs:
            data[key] = kwargs[key]
        self.api_call("/api/services/{}".format(service), data, post=True)

    def api_call(self, endpoint, data_in=None, post=False):
        """
        Make an API call to Home Assistant.

        :param endpoint: The API endpoint to call.
        :param data_in: The data to send in the body of the request.
        :param post: True if this is a POST request, False for GET.
        :return: The response from the API.
        """
        url = self.ha_url + endpoint
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
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Failed to decode response {} from {}".format(response, url))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: Timeout from {}".format(url))
            data = None
        return data
