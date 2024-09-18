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
from config import TIME_FORMAT_HA, TIMEOUT

TIME_FORMAT_DB = "%Y-%m-%dT%H:%M:%S.%f"

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
        self.db = None
        self.db_cursor = None
        self.websocket_active = False

        self.base = base
        self.log = base.log
        self.state_data = {}
        self.slug = None

        if not self.ha_key:
            self.log("Warn: ha_key or SUPERVISOR_TOKEN not found, you can set ha_url/ha_key in apps.yaml. Will use direct HA API")
        else:
            check = self.api_call("/api/")
            if not check:
                self.log("Warn: Unable to connect directly to Home Assistant at {}, please check your configuration of ha_url/ha_key".format(self.ha_url))
                self.ha_key = None
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
                    # Open the SQL Lite database called predbat.db
                    self.log("Info: Opening database")
                    self.db = sqlite3.connect(self.base.config_root + "/predbat.db")
                    self.db_cursor = self.db.cursor()
                    self.log("Info: Clean data older than {} days".format(self.db_days))
                    self.cleanup_db()

    def get_slug(self):
        """
        Get the add-on slug.
        """
        return self.slug

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
                                                    entity_id = new_state.get("entity_id", None)
                                                    if entity_id:
                                                        self.update_state_item(new_state, entity_id)
                                                    else:
                                                        self.log("Warn: Web Socket state_changed event has no entity_id {}".format(new_state))
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
        Get state from cached HA data (or from AppDaemon if used)
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
            self.update_state_item(item, entity_id)

    def update_state_item(self, item, entity_id):
        """
        Update state table for item
        """
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
                entity_id = item.get("entity_id", None)
                if entity_id:
                    self.update_state_item(item, entity_id)
                else:
                    self.log("Warn: Failed to update state data from HA, item has no entity_id {}".format(item))
        else:
            self.log("Warn: Failed to update state data from HA")

    def get_history_db(self, sensor, now, days=30):
        """
        Get the history for a sensor from the SQLLite database.
        """
        if not self.db_enable:
            return self.get_history(sensor, now, days=days)
        
        start = now - timedelta(days=days)
        table_name = sensor.replace(".", "__")
        # Check if table exists
        # If not then return empty history
        self.db_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        res = self.db_cursor.fetchone()
        if not res:
            self.log("Warn: Table {} does not exist, trying HA data".format(table_name))
            return self.get_history(sensor, now, days=days)

        # Get the history for the sensor, sorted by datetime
        self.db_cursor.execute("SELECT datetime, state, attributes FROM {} WHERE datetime >= ? ORDER BY datetime".format(table_name), (start.strftime(TIME_FORMAT_DB), ))
        res = self.db_cursor.fetchall()
        history = []
        for item in res:
            state = item[1]
            try:
                state = json.loads(state)
            except json.JSONDecodeError:
                pass
            attributes = {}
            try:
                attributes = json.loads(item[2])
            except json.JSONDecodeError:
                pass

            history.append({"last_updated": item[0] + 'Z', "state": state, "attributes": attributes})
        return [history]

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
    
    def set_state_db(self, entity_id, state, attributes):
        """
        Records the state of a predbat entity into the SQLLite database
        There is one table per Entity created with history recorded
        """
        if not self.db_enable:
            return
        
        # Convert time to GMT+0
        now_utc = self.base.now_utc_real
        now_utc = now_utc.replace(tzinfo=None) - timedelta(hours=now_utc.utcoffset().seconds // 3600)
        now_utc_txt = now_utc.strftime(TIME_FORMAT_DB)
        system = {}

        # Create a table for the entity if it does not exist
        table_name = entity_id.replace(".", "__")
        self.db_cursor.execute("CREATE TABLE IF NOT EXISTS {} (datetime TEXT PRIMARY KEY, state TEXT, attributes TEXT, system TEXT)".format(table_name))

        attributes_record = {}
        for key in attributes:
            if key not in ['friendly_name', 'icon', 'unit_of_measurement', 'results', 'html', 'state_class', 'options', 'detailedForecast']:
                attributes_record[key] = attributes[key]
        attributes_record_json = json.dumps(attributes_record)

        # Record the state of the entity
        # If the entity value and attributes are unchanged then don't record the new state
        self.db_cursor.execute("SELECT datetime, state, attributes FROM {} ORDER BY datetime DESC LIMIT 1".format(table_name))
        last_record = self.db_cursor.fetchone()
        if last_record:
            last_datetime = datetime.strptime(last_record[0], TIME_FORMAT_DB)
            last_state = last_record[1]
            last_attributes = last_record[2]
            if last_state == str(state) and last_attributes == attributes_record_json:
                return

            # Avoid duplicate datetime records
            if last_datetime == now_utc:
                # Delete previous record with this datetime
                self.db_cursor.execute("DELETE FROM {} WHERE datetime = ?".format(table_name), (now_utc_txt,))

        # Insert the new state record
        self.db_cursor.execute("INSERT INTO {} VALUES (?, ?, ?, ?)".format(table_name), (now_utc.strftime(TIME_FORMAT_DB), json.dumps(state), attributes_record_json, json.dumps(system)))
        self.db.commit()

    def cleanup_db(self):
        """
        This searches all tables for data older than N days and deletes it
        """
        if not self.db_enable:
            return

        self.db_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = self.db_cursor.fetchall()
        for table in tables:
            table_name = table[0]
            self.db_cursor.execute("DELETE FROM {} WHERE datetime < ?".format(table_name), (self.base.now_utc_real - timedelta(days=self.db_days),))
            self.db.commit()

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
            data["unrecorded_attributes"] = ["results"]
        self.api_call("/api/states/{}".format(entity_id), data, post=True)
        self.update_state(entity_id)
        self.set_state_db(entity_id, state, attributes)

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
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Failed to decode response {} from {}".format(response, url))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: Timeout from {}".format(url))
            data = None
        return data
