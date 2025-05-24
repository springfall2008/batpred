# -----------------------------------------------------------------------------
# Database Manager for Predbat Home Battery System
# This module handles all SQL Lite database operations.
# -----------------------------------------------------------------------------

from datetime import timedelta
import asyncio
import time
import traceback
import threading
from db_engine import DatabaseEngine, TIME_FORMAT_DB


class DatabaseManager:
    def __init__(self, base, db_days):
        self.base = base
        self.log = base.log
        self.db_days = db_days
        self.stop_thread = False
        self.db_queue = []
        self.queue_id = 0
        self.queue_results = {}
        self.sync_event = threading.Event()
        self.async_event = asyncio.Event()
        self.return_event = threading.Event()

    def bridge_event(self, loop):
        self.sync_event.wait()
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

        while not self.stop_thread:
            if not self.db_queue:
                await self.async_event.wait()
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
                elif command == "get_all_entities":
                    entities = self.db_engine._get_all_entities_db()
                    self.queue_results[queue_id] = entities
                    self.return_event.set()  # Notify that the result is ready
                elif command == "get_state":
                    state = self.db_engine._get_state_db(info["entity_id"])
                    self.queue_results[queue_id] = state
                    self.return_event.set()  # Notify that the result is ready
                elif command == "stop":
                    self.stop_thread = True
                    self.log("db_manager: stopping")

            except Exception as e:
                self.log(f"Error in database thread: {e}")
                self.log("Error: " + traceback.format_exc())

        self.db_engine._close()
        self.log("db_manager: Stopped")

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

    def stop(self):
        """
        Close the database connection
        """
        self.stop_thread = True
        queue_id = self.queue_id
        self.queue_id += 1
        self.send_via_ipc("stop", {}, expect_response=False)

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
            now_utc = self.base.now_utc_real

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
