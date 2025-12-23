# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Database Manager for Predbat Home Battery System
# This module handles all SQL Lite database operations.
# -----------------------------------------------------------------------------

from datetime import timedelta, datetime, timezone
import asyncio
import time
import traceback
import threading
from db_engine import DatabaseEngine, TIME_FORMAT_DB
from component_base import ComponentBase


class DatabaseManager(ComponentBase):
    def initialize(self, db_enable, db_days, db_commit_interval=0):
        self.db_days = db_days
        self.db_queue = []
        self.queue_id = 0
        self.queue_results = {}
        self.sync_event = threading.Event()
        self.async_event = asyncio.Event()
        self.return_event = threading.Event()
        self.api_started = False
        self.last_success_timestamp = None
        self.commit_interval = self.get_arg("db_commit_interval", 0)
        self.log("db_manager: Commit interval set to {} seconds".format(self.commit_interval))

    def bridge_event(self, loop):
        """
        This function runs in a separate thread to bridge the event loop and the database manager.
        It waits for the sync_event to be set and then sets the async_event to notify the main loop.
        This allows the database manager to process commands asynchronously.
        """
        while not self.api_stop:
            self.sync_event.wait()
            self.sync_event.clear()  # Clear the event to allow the loop to continue
            loop.call_soon_threadsafe(self.async_event.set)

    async def start(self):
        """
        Initialize the database and clean up old data
        """

        self.db_engine = DatabaseEngine(self.base, self.db_days)

        loop = asyncio.get_running_loop()

        # Start the bridge in a thread
        threading.Thread(target=self.bridge_event, args=(loop,), daemon=True).start()

        self.log("db_manager: Started")
        self.api_started = True

        last_commit_time = time.time()

        while not self.api_stop:
            # Check commit timer
            now = time.time()
            if self.commit_interval > 0 and (now - last_commit_time >= self.commit_interval):
                 self.db_engine.commit()
                 last_commit_time = now

            if not self.db_queue:
                wait_time = 0.1
                if self.commit_interval > 0:
                    remaining = self.commit_interval - (time.time() - last_commit_time)
                    wait_time = max(0.1, remaining)

                try:
                    await asyncio.wait_for(self.async_event.wait(), timeout=wait_time)
                except asyncio.TimeoutError:
                    pass
                self.async_event.clear()
                continue
            else:
                item = self.db_queue.pop(0)

            try:
                queue_id, command, info = item[0], item[1], item[2]
                if command == "get_history":
                    history = self.db_engine._get_history_db(info["sensor"], info["now"], info["days"])
                    self.queue_results[queue_id] = history
                    self.return_event.set()  # Notify that the result is ready
                elif command == "set_state":
                    self.db_engine._set_state_db(info["entity_id"], info["state"], info["attributes"], timestamp=info["timestamp"])
                    if self.commit_interval <= 0:
                        self.db_engine.commit()
                elif command == "get_all_entities":
                    entities = self.db_engine._get_all_entities_db()
                    self.queue_results[queue_id] = entities
                    self.return_event.set()  # Notify that the result is ready
                elif command == "get_state":
                    state = self.db_engine._get_state_db(info["entity_id"])
                    self.queue_results[queue_id] = state
                    self.return_event.set()  # Notify that the result is ready
                elif command == "stop":
                    self.api_stop = True
                    self.log("db_manager: stopping")

                self.last_success_timestamp = datetime.now(timezone.utc)

            except Exception as e:
                self.log(f"Error in database thread: {e}")
                self.log("Error: " + traceback.format_exc())

        self.db_engine._close()
        self.log("db_manager: Stopped")
        self.api_started = False

    def send_via_ipc(self, command, info, expect_response=False):
        """
        Send a command to the database manager thread via IPC
        """
        queue_id = self.queue_id
        self.queue_id += 1
        self.db_queue.append((queue_id, command, info))
        self.return_event.clear()  # Clear the event before waiting
        self.sync_event.set()  # Notify the thread to process the queue

        if expect_response:
            count = 0.0
            while (queue_id not in self.queue_results) and count < 15.0:
                self.return_event.wait(0.1)
                if queue_id not in self.queue_results:
                    time.sleep(0.1)  # Wait a bit before checking again
                count += 0.1
            if queue_id in self.queue_results:
                result = self.queue_results[queue_id]
                del self.queue_results[queue_id]
                return result
            else:
                self.log("Error: No response received for command '{}' after waiting".format(command))
                return None
        return None

    async def stop(self):
        """
        Close the database connection
        """
        self.api_stop = True
        self.send_via_ipc("stop", {}, expect_response=False)
        self.api_started = False
        self.log("db_manager: stop command sent")

    def get_state_db(self, entity_id):
        """
        Get entity current state but run it from the DB thread
        """
        # Queue the request using IPC and wait for the result

        result = self.send_via_ipc("get_state", {"entity_id": entity_id}, expect_response=True)
        return result

    def get_all_entities_db(self):
        """
        Get all entity names from the SQLLite database but run it from the DB thread
        """
        # Queue the request using IPC and wait for the result

        result = self.send_via_ipc("get_all_entities", {}, expect_response=True)
        return result

    def set_state_db(self, entity_id, state, attributes, timestamp=None):
        if timestamp is not None:
            now_utc = timestamp
        else:
            now_utc = self.now_utc_exact

        # Convert time to GMT+0
        now_utc = now_utc.replace(tzinfo=None) - timedelta(hours=now_utc.utcoffset().seconds // 3600)
        self.send_via_ipc("set_state", {"entity_id": entity_id, "state": state, "attributes": attributes, "timestamp": now_utc})

        item = {"last_changed": now_utc.strftime(TIME_FORMAT_DB), "state": state, "attributes": attributes}
        return item

    def get_history_db(self, sensor, now, days=30):
        """
        Get history but run it from the DB thread
        """
        # Queue the request using IPC and wait for the result
        result = self.send_via_ipc("get_history", {"sensor": sensor, "now": now, "days": days}, expect_response=True)
        return result
