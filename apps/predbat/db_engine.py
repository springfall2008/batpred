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
import hashlib
from datetime import datetime, timedelta

TIME_FORMAT_DB = "%Y-%m-%dT%H:%M:%S.%f"


class DatabaseEngine:
    def __init__(self, base, db_days):
        self.base = base
        self.log = base.log
        self.db_days = db_days

        self.db = sqlite3.connect(self.base.config_root + "/predbat.db")
        self.db_cursor = self.db.cursor()

        self._cleanup_db()
        self.log("db_engine: Started")

    def _entity_hash(self, entity_id):
        """
        Generate a consistent hash for an entity_id
        """
        # Use SHA256 hash and take first 8 bytes as integer for 64-bit signed integer
        # This gives us a deterministic ID that's the same across all instances
        hash_bytes = hashlib.sha256(entity_id.encode('utf-8')).digest()[:8]
        return int.from_bytes(hash_bytes, byteorder='big', signed=True)

    def _close(self):
        """
        Close the database connection
        """
        if self.db:
            self.db.close()
            self.log("db_engine: Closed")
            self.db = None

    def _cleanup_db(self):
        """
        This searches all tables for data older than N days and deletes it
        """
        # Modified schema: latest table uses hash as primary key, stores entity_id
        # history table references latest via entity_hash foreign key
        self.db_cursor.execute("""
                               CREATE TABLE IF NOT EXISTS latest (
                                                                     entity_hash INTEGER PRIMARY KEY,
                                                                     entity_id TEXT UNIQUE NOT NULL,
                                                                     datetime TEXT,
                                                                     state TEXT,
                                                                     attributes TEXT,
                                                                     system TEXT,
                                                                     keep TEXT
                               )
                               """)

        self.db_cursor.execute("""
                               CREATE TABLE IF NOT EXISTS history (
                                                                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                                      entity_hash INTEGER,
                                                                      datetime TEXT,
                                                                      state TEXT,
                                                                      attributes TEXT,
                                                                      system TEXT,
                                                                      keep TEXT,
                                                                      FOREIGN KEY (entity_hash) REFERENCES latest(entity_hash)
                               )
                               """)

        # Create index on entity_hash for faster history queries
        self.db_cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_entity_hash ON history(entity_hash)")
        self.db_cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_datetime ON history(datetime)")

        self.db_cursor.execute(
            "DELETE FROM history WHERE datetime < ? AND keep != ?",
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
        entity_hash = self._entity_hash(entity_id)

        self.db_cursor.execute("SELECT datetime, state, attributes FROM latest WHERE entity_hash=?", (entity_hash,))
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

    def _get_all_entities_db(self):
        """
        Get all entity names from the SQLLite database
        """
        self.db_cursor.execute("SELECT entity_id FROM latest")
        rows = self.db_cursor.fetchall()
        return [row[0] for row in rows]

    def _set_state_db(self, entity_id, state, attributes, timestamp):
        """
        Records the state of a predbat entity into the SQLLite database
        Uses hash-based entity IDs for consistent references across instances
        """
        state = str(state)
        entity_id = entity_id.lower()
        entity_hash = self._entity_hash(entity_id)

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

        # Check if the entity value and attributes are unchanged
        self.db_cursor.execute("SELECT datetime, state, attributes, system, keep FROM history WHERE entity_hash=? ORDER BY datetime DESC LIMIT 1", (entity_hash,))
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

        # Insert the new state record using upsert
        try:
            # Upsert into the latest table
            self.db_cursor.execute(
                """
                INSERT INTO latest (entity_hash, entity_id, datetime, state, attributes, system, keep)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_hash) DO UPDATE SET
                                                       entity_id=excluded.entity_id,
                                                       datetime=excluded.datetime,
                                                       state=excluded.state,
                                                       attributes=excluded.attributes,
                                                       system=excluded.system,
                                                       keep=excluded.keep
                """,
                (
                    entity_hash,
                    entity_id,
                    now_utc_txt,
                    state,
                    attributes_record_full_json,
                    system_json,
                    keep,
                ),
            )

            # Insert into history table using entity_hash
            self.db_cursor.execute(
                "INSERT INTO history (entity_hash, datetime, state, attributes, system, keep) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entity_hash,
                    now_utc_txt,
                    state,
                    attributes_record_json,
                    system_json,
                    keep,
                ),
            )

            self.db.commit()
        except sqlite3.IntegrityError:
            self.log("Warn: SQL Integrity error inserting data for {}".format(entity_id))

    def _get_history_db(self, sensor, now, days=30):
        """
        Get the history for a sensor from the SQLLite database.
        """
        start = now - timedelta(days=days)
        sensor = sensor.lower()
        entity_hash = self._entity_hash(sensor)

        # Get the history for the sensor, sorted by datetime
        self.db_cursor.execute(
            "SELECT datetime, state, attributes FROM history WHERE entity_hash = ? AND datetime >= ? ORDER BY datetime",
            (
                entity_hash,
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