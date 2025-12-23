# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Database Engine for Predbat Home Battery System
# This module handles all SQL Lite database operations.
# -----------------------------------------------------------------------------

import sqlite3
import json
from datetime import datetime, timedelta

TIME_FORMAT_DB = "%Y-%m-%dT%H:%M:%S.%f"


class DatabaseEngine:
    def __init__(self, base, db_days):
        self.base = base
        self.log = base.log
        self.db_days = db_days

        self.db = sqlite3.connect(self.base.config_root + "/predbat.db")
        self.db_cursor = self.db.cursor()
        self.db_cursor.execute("PRAGMA journal_mode=WAL")
        self.db_cursor.execute("PRAGMA synchronous=NORMAL")
        self.entity_id_cache = {}

        self._cleanup_db()
        self.log("db_engine: Started")

    def _close(self):
        """
        Close the database connection
        """
        if self.db:
            self.db.close()
            self.log("db_engine: Closed")
            self.db = None

    def commit(self):
        """
        Commit changes to the database
        """
        if self.db:
            self.db.commit()

    def _cleanup_db(self):
        """
        This searches all tables for data older than N days and deletes it
        """
        self.db_cursor.execute("CREATE TABLE IF NOT EXISTS entities (entity_index INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT KEY UNIQUE)")
        self.db_cursor.execute("CREATE TABLE IF NOT EXISTS states (id INTEGER PRIMARY KEY AUTOINCREMENT, datetime TEXT KEY, entity_index INTEGER KEY, state TEXT, attributes TEXT, system TEXT, keep TEXT KEY)")
        self.db_cursor.execute("CREATE TABLE IF NOT EXISTS latest (entity_index INTEGER PRIMARY KEY, datetime TEXT KEY, state TEXT, attributes TEXT, system TEXT, keep TEXT KEY)")
        self.db_cursor.execute(
            "DELETE FROM states WHERE datetime < ? AND keep != ?",
            (
                self.base.now_utc_real - timedelta(days=self.db_days),
                "D",
            ),
        )
        self.db.commit()

    def _get_state_db(self, entity_id):
        """
        Get entity current state from the SQLLite database
        """
        entity_id = entity_id.lower()
        entity_index = self._get_entity_index_db(entity_id)
        if not entity_index:
            return None

        self.db_cursor.execute("SELECT datetime, state, attributes FROM latest WHERE entity_index=?", (entity_index,))
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

    def _get_entity_index_db(self, entity_id):
        """
        Get the entity index from the SQLLite database
        """
        if entity_id in self.entity_id_cache:
            return self.entity_id_cache[entity_id]

        self.db_cursor.execute("SELECT entity_index FROM entities WHERE entity_name=?", (entity_id,))
        res = self.db_cursor.fetchone()
        if res:
            self.entity_id_cache[entity_id] = res[0]
            return res[0]
        else:
            return None

    def _get_all_entities_db(self):
        """
        Get all entity names from the SQLLite database
        """
        self.db_cursor.execute("SELECT entity_name FROM entities")
        rows = self.db_cursor.fetchall()
        return [row[0] for row in rows]

    def _set_state_db(self, entity_id, state, attributes, timestamp):
        """
        Records the state of a predbat entity into the SQLLite database
        There is one table per Entity created with history recorded
        """
        state = str(state)

        # Put the entity_id into entities table if its not in already
        self.db_cursor.execute("INSERT OR IGNORE INTO entities (entity_name) VALUES (?)", (entity_id,))
        entity_index = self._get_entity_index_db(entity_id)

        # Convert time to GMT+0
        now_utc = timestamp
        now_utc_txt = now_utc.strftime(TIME_FORMAT_DB)

        system = {}

        attributes_record = {}
        attributes_record_full = {}
        for key in attributes:
            value = attributes[key]
            # Ignore large fields which will increase the database size
            if len(str(value)) < 128:
                attributes_record[key] = attributes[key]
            attributes_record_full[key] = attributes[key]
        attributes_record_json = json.dumps(attributes_record)
        attributes_record_full_json = json.dumps(attributes_record_full)
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
            self.db_cursor.execute("DELETE FROM states WHERE entity_index = ? AND datetime = ?", (entity_index, now_utc_txt))
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
            # Upsert into the latest table (replace if exists, insert if not)
            self.db_cursor.execute(
                """
                INSERT INTO latest (entity_index, datetime, state, attributes, system, keep)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_index) DO UPDATE SET
                    datetime=excluded.datetime,
                    state=excluded.state,
                    attributes=excluded.attributes,
                    system=excluded.system,
                    keep=excluded.keep
                """,
                (
                    entity_index,
                    now_utc_txt,
                    state,
                    attributes_record_full_json,
                    system_json,
                    keep,
                ),
            )
            self.db.commit()  # Commit immediately to avoid data loss; WAL mode keeps this relatively cheap
        except sqlite3.IntegrityError:
            self.log("Warn: SQL Integrity error inserting data for {}".format(entity_id))

    def _get_history_db(self, sensor, now, days=30):
        """
        Get the history for a sensor from the SQLLite database.
        """
        start = now - timedelta(days=days)

        entity_index = self._get_entity_index_db(sensor)
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
