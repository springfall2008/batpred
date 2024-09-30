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
from config import TIME_FORMAT_HA, TIMEOUT, TIME_FORMAT_SECONDS

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
        self.db_mirror_ha = base.args.get("db_mirror_ha", False)
        self.db_primary = base.args.get("db_primary", False)
        self.db_mirror_list = {}
        self.db = None
        self.db_cursor = None
        self.db_mirror_updates = []
        self.websocket_active = False

        self.base = base
        self.log = base.log
        self.state_data = {}
        self.slug = None

        if not self.ha_key:
            if not (self.db_enable and self.db_primary):
                self.log("Warn: ha_key or SUPERVISOR_TOKEN not found, you can set ha_url/ha_key in apps.yaml. Will use direct HA API")
            else:
                self.log("Info: Using SQL Lite database as primary data source")
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
        if entity_id:
            self.db_mirror_list[entity_id.lower()] = True

        if not self.ha_key and not (self.db_enable and self.db_primary):
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

    def update_state_db(self, entity_id):
        """
        Update state for entity_id from the SQLLite database
        """
        self.db_mirror_list[entity_id.lower()] = True
        item = self.get_state_db(entity_id)
        if item:
            self.update_state_item(item, entity_id, nodb=True)

    def update_state(self, entity_id):
        """
        Update state for entity_id
        """
        self.db_mirror_list[entity_id.lower()] = True

        if self.db_primary and self.db_enable:
            self.update_state_db(entity_id)
            return

        if not self.ha_key:
            return

        item = self.api_call("/api/states/{}".format(entity_id))
        if item:
            self.update_state_item(item, entity_id)

    def get_state_db(self, entity_id):
        """
        Get entity current state from the SQLLite database
        """
        if not self.db_enable:
            return None

        entity_id = entity_id.lower()
        entity_index = self.get_entity_index_db(entity_id)
        if not entity_index:
            return None

        self.db_cursor.execute("SELECT datetime, state, attributes FROM states WHERE entity_index=? ORDER BY datetime DESC LIMIT 1", (entity_index,))
        res = self.db_cursor.fetchone()
        if res:
            state = res[1]
            attributes = {}
            try:
                attributes = json.loads(res[2])
            except json.JSONDecodeError:
                pass
            return {"last_updated": res[0] + "Z", "state": state, "attributes": attributes}
        else:
            return None

    def update_state_item(self, item, entity_id, nodb=False):
        """
        Update state table for item
        """
        entity_id = entity_id.lower()
        attributes = item["attributes"]
        last_changed = item["last_changed"]
        state = item["state"]
        self.state_data[entity_id] = {"state": state, "attributes": attributes, "last_changed": last_changed}
        if not nodb and self.db_mirror_ha and (entity_id in self.db_mirror_list):
            self.db_mirror_updates.append({"entity_id": entity_id, "state": state, "attributes": attributes, "timestamp": self.base.now_utc_real})

    def db_tick(self):
        """
        Update the database with any new data
        """
        if not self.db_enable:
            return

        if self.db_mirror_updates:
            mirror_list = self.db_mirror_updates[:]
            self.db_mirror_updates = []
            for item in mirror_list:
                self.set_state_db(item["entity_id"], item["state"], item["attributes"], timestamp=item["timestamp"])

    def update_states_db(self):
        """
        Update the state data from the SQL Lite database
        """
        if not self.db_enable:
            return

        self.db_cursor.execute("SELECT entity_name FROM entities")
        tables = self.db_cursor.fetchall()
        for table in tables:
            entity_name = table[0]
            self.update_state_db(entity_name)

    def update_states(self):
        """
        Update the state data from Home Assistant.
        """

        if self.db_primary and self.db_enable:
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

    def get_history_db(self, sensor, now, days=30):
        """
        Get the history for a sensor from the SQLLite database.
        """
        if not self.db_enable:
            return None

        self.db_mirror_list[sensor.lower()] = True

        start = now - timedelta(days=days)

        entity_index = self.get_entity_index_db(sensor)
        if not entity_index:
            self.log("Warn: Entity {} does not exist".format(sensor))
            return None

        # Get the history for the sensor, sorted by datetime
        self.db_cursor.execute(
            "SELECT datetime, state, attributes FROM states WHERE entity_index = ? AND datetime >= ? ORDER BY datetime",
            (
                entity_index,
                start.strftime(TIME_FORMAT_DB),
            ),
        )
        res = self.db_cursor.fetchall()
        history = []
        for item in res:
            state = item[1]
            attributes = {}
            try:
                attributes = json.loads(item[2])
            except json.JSONDecodeError:
                pass

            history.append({"last_updated": item[0] + "Z", "state": state, "attributes": attributes})
        return [history]

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
            return self.get_history_db(sensor, now, days=days)

        if not self.ha_key:
            return self.base.get_history_ad(sensor, days=days)

        start = now - timedelta(days=days)
        end = now
        res = self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        return res

    def get_entity_index_db(self, entity_id):
        """
        Get the entity index from the SQLLite database
        """
        if not self.db_enable:
            return None

        self.db_cursor.execute("SELECT entity_index FROM entities WHERE entity_name=?", (entity_id,))
        res = self.db_cursor.fetchone()
        if res:
            return res[0]
        else:
            return None

    def set_state_db_later(self, entity_id, state, attributes, timestamp=None):
        if not self.db_enable:
            return

        if timestamp:
            now_utc = timestamp
        else:
            now_utc = self.base.now_utc_real

        # Convert time to GMT+0
        now_utc = now_utc.replace(tzinfo=None) - timedelta(hours=now_utc.utcoffset().seconds // 3600)
        now_utc_txt = now_utc.strftime(TIME_FORMAT_DB)

        item = {"last_changed": now_utc_txt, "state": state, "attributes": attributes}
        self.update_state_item(item=item, entity_id=entity_id, nodb=True)
        return

    def set_state_db(self, entity_id, state, attributes, timestamp=None):
        """
        Records the state of a predbat entity into the SQLLite database
        There is one table per Entity created with history recorded
        """
        if not self.db_enable:
            return

        state = str(state)

        # Put the entity_id into entities table if its not in already
        self.db_cursor.execute("INSERT OR IGNORE INTO entities (entity_name) VALUES (?)", (entity_id,))
        self.db.commit()
        entity_index = self.get_entity_index_db(entity_id)

        # Use of last_changed allows the state to be recorded at a different time
        if timestamp:
            now_utc = timestamp
        else:
            now_utc = self.base.now_utc_real

        # Convert time to GMT+0
        now_utc = now_utc.replace(tzinfo=None) - timedelta(hours=now_utc.utcoffset().seconds // 3600)
        now_utc_txt = now_utc.strftime(TIME_FORMAT_DB)

        system = {}

        attributes_record = {}
        for key in attributes:
            value = attributes[key]
            # Ignore large fields which will increase the database size
            if len(str(value)) < 128:
                attributes_record[key] = attributes[key]
        attributes_record_json = json.dumps(attributes_record)
        system_json = json.dumps(system)

        # Record the state of the entity
        # If the entity value and attributes are unchanged then don't record the new state
        self.db_cursor.execute("SELECT datetime, state, attributes, system, keep FROM states WHERE entity_index=? ORDER BY datetime DESC LIMIT 1", (entity_index,))
        last_record = self.db_cursor.fetchone()
        keep = "D"
        if last_record:
            last_datetime = datetime.strptime(last_record[0], TIME_FORMAT_DB)
            last_state = last_record[1]
            last_attributes = last_record[2]
            last_system = last_record[3]
            last_keep = last_record[4]
            if last_state == state and last_attributes == attributes_record_json and last_system == system_json:
                return

            # Compute keep value
            last_datetime_datestr = last_datetime.strftime("%Y-%m-%d")
            now_datetime_datestr = now_utc.strftime("%Y-%m-%d")
            if last_datetime_datestr == now_datetime_datestr:
                if last_datetime.hour == now_utc.hour:
                    keep = "I"
                else:
                    keep = "H"

        # Insert the new state record
        try:
            self.db_cursor.execute(
                "DELETE FROM states WHERE entity_index = ? AND datetime = ?".format(entity_id),
                (
                    entity_index,
                    now_utc_txt,
                ),
            )
            self.db_cursor.execute(
                "INSERT INTO states (datetime, entity_index, state, attributes, system, keep) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now_utc_txt,
                    entity_index,
                    state,
                    attributes_record_json,
                    system_json,
                    keep,
                ),
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            self.log("Warn: SQL Integrity error inserting data for {}".format(entity_id))

    def cleanup_db(self):
        """
        This searches all tables for data older than N days and deletes it
        """
        if not self.db_enable:
            return

        self.db_cursor.execute("CREATE TABLE IF NOT EXISTS entities (entity_index INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT KEY UNIQUE)")
        self.db_cursor.execute(
            "CREATE TABLE IF NOT EXISTS states (id INTEGER PRIMARY KEY AUTOINCREMENT, datetime TEXT KEY, entity_index INTEGER KEY, state TEXT, attributes TEXT, system TEXT, keep TEXT KEY)"
        )
        self.db_cursor.execute(
            "DELETE FROM states WHERE datetime < ? AND keep != ?",
            (
                self.base.now_utc_real - timedelta(days=self.db_days),
                "D",
            ),
        )
        self.db.commit()

    def set_state(self, entity_id, state, attributes={}):
        """
        Set the state of an entity in Home Assistant.
        """
        self.db_mirror_list[entity_id] = True

        if self.db_mirror_ha or self.db_primary:
            self.set_state_db_later(entity_id, state, attributes)

        if self.ha_key:
            data = {"state": state}
            if attributes:
                data["attributes"] = attributes
                data["unrecorded_attributes"] = ["results"]
            self.api_call("/api/states/{}".format(entity_id), data, post=True)
            self.update_state(entity_id)
        elif not self.db_primary:
            if attributes:
                return self.base.set_state(entity_id, state=state, attributes=attributes)
            else:
                return self.base.set_state(entity_id, state=state)

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
