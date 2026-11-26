# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the memory benchmark's fixture database export, on a small synthetic live database."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "apps" / "predbat"))

import fixture_db  # noqa: E402
from db_engine import DatabaseEngine, TIME_FORMAT_DB  # noqa: E402

T0 = datetime(2026, 11, 26, 8, 0, 0)

LIVE_ARGS = {
    "load_today": ["sensor.real_load_today"],
    "battery_power": ["sensor.real_battery_power"],
    "battery_power_invert": [True],
    "soc_max": ["sensor.real_capacity"],
    "pv_today": "sensor.real_pv_today",
}


class StubBase:
    """Just enough of PredBat for DatabaseEngine to open a database."""

    def __init__(self, root):
        self.config_root = str(root)
        self.now_utc_real = T0
        self.log = lambda *args, **kwargs: None


def make_live_db(path):
    """Write a synthetic live predbat.db through the real DatabaseEngine and return its path."""
    engine = DatabaseEngine(StubBase(path), 30)
    rows = [
        # A daily load counter that resets at midnight, with one row older than the 7-day window
        ("sensor.real_load_today", T0 - timedelta(days=9), "4.0", {"unit_of_measurement": "kWh"}),
        ("sensor.real_load_today", T0 - timedelta(hours=10), "1.0", {"unit_of_measurement": "kWh"}),
        ("sensor.real_load_today", T0 - timedelta(hours=9), "2.0", {"unit_of_measurement": "kWh"}),
        ("sensor.real_load_today", T0 - timedelta(hours=7), "0.0", {"unit_of_measurement": "kWh"}),
        ("sensor.real_load_today", T0 - timedelta(hours=1), "0.5", {"unit_of_measurement": "kWh"}),
        # Battery power in kW with an inverted sign (positive = charging on this inverter)
        ("sensor.real_battery_power", T0 - timedelta(hours=2), "1.5", {"unit_of_measurement": "kW"}),
        # Battery capacity in Wh
        ("sensor.real_capacity", T0 - timedelta(hours=3), "9500", {"unit_of_measurement": "Wh"}),
        # A Predbat setting (kept), two Predbat outputs (dropped) and an unrelated entity (kept)
        ("switch.predbat_expert_mode", T0 - timedelta(hours=5), "on", {"friendly_name": "Expert Mode"}),
        ("predbat.plan_history", T0 - timedelta(hours=1), "x", {"history": "big"}),
        ("sensor.predbat_cost_today", T0 - timedelta(hours=1), "1.23", {}),
        ("sensor.octopus_rates", T0 - timedelta(hours=4), "12.5", {"account": "A-1234"}),
        ("zone.home", T0 - timedelta(hours=4), "0", {"latitude": 52.1, "longitude": -1.6, "friendly_name": "Home"}),
    ]
    for entity_id, when, state, attributes in rows:
        engine._set_state_db(entity_id, state, attributes, when)
    engine._close()
    return Path(path) / "predbat.db"


def query(db, sql, args=()):
    """Run one query against a database file and return every row."""
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def history(db, entity_id):
    """Return [(datetime text, state)] for one entity, oldest first."""
    return query(db, "SELECT s.datetime, s.state FROM states s JOIN entities e USING(entity_index) WHERE e.entity_name = ? ORDER BY s.datetime", (entity_id,))


class TestDailyToLifetime(unittest.TestCase):
    """daily_to_lifetime turns a counter that resets at midnight into one that only rises."""

    def test_daily_to_lifetime_carries_across_a_reset(self):
        """0, 1, 2, then a reset to 0, then 0.5 becomes 0, 1, 2, 2, 2.5 with a total of 2.5."""
        times = [T0 + timedelta(hours=n) for n in range(5)]
        series, total = fixture_db.daily_to_lifetime(list(zip(times, [0.0, 1.0, 2.0, 0.0, 0.5])))
        self.assertEqual([value for _, value in series], [0.0, 1.0, 2.0, 2.0, 2.5])
        self.assertEqual(total, 2.5)

    def test_daily_to_lifetime_rebases_to_zero_and_ignores_jitter(self):
        """The series starts at 0, and a small dip is jitter that holds the value rather than a reset."""
        times = [T0 + timedelta(hours=n) for n in range(4)]
        series, total = fixture_db.daily_to_lifetime(list(zip(times, [5.0, 6.0, 5.98, 7.0])))
        self.assertEqual([round(value, 6) for _, value in series], [0.0, 1.0, 1.0, 2.0])
        self.assertEqual(round(total, 6), 2.0)

    def test_daily_to_lifetime_of_nothing(self):
        """An empty series has no rows and a zero total."""
        self.assertEqual(fixture_db.daily_to_lifetime([]), ([], 0.0))


class TestNames(unittest.TestCase):
    """The settings list and the output test separate what the export keeps from what it drops."""

    def test_settings_entities_names(self):
        """Settings are named <type>.<prefix>_<name>; update items are not settings."""
        settings = fixture_db.settings_entities("predbat")
        self.assertIn("switch.predbat_expert_mode", settings)
        self.assertNotIn("update.predbat_version", settings)

    def test_is_predbat_output_keeps_settings_and_drops_outputs(self):
        """predbat.* and other prefixed entities are outputs unless they are settings; other entities are not."""
        settings = {"switch.predbat_expert_mode"}
        self.assertTrue(fixture_db.is_predbat_output("predbat.plan_history", "predbat", settings))
        self.assertTrue(fixture_db.is_predbat_output("sensor.predbat_cost_today", "predbat", settings))
        self.assertFalse(fixture_db.is_predbat_output("switch.predbat_expert_mode", "predbat", settings))
        self.assertFalse(fixture_db.is_predbat_output("sensor.octopus_rates", "predbat", settings))

    def test_build_remap_maps_live_args_to_dummy_sensors(self):
        """Each live inverter setting maps to the dummy's sensor of the same kind, taking a list's first entity."""
        remap = {r.real_entity: r for r in fixture_db.build_remap(LIVE_ARGS, "predbat")}
        self.assertEqual(remap["sensor.real_load_today"], fixture_db.Remap("sensor.real_load_today", "sensor.predbat_dummy_load_lifetime", "lifetime", False))
        self.assertEqual(remap["sensor.real_pv_today"].dummy_entity, "sensor.predbat_dummy_pv_lifetime")
        self.assertEqual(remap["sensor.real_battery_power"], fixture_db.Remap("sensor.real_battery_power", "sensor.predbat_dummy_battery_power", "power_w", True))
        self.assertEqual(remap["sensor.real_capacity"].kind, "gauge_kwh")


class TestTemplates(unittest.TestCase):
    """Templated live settings ({geserial}) resolve against the database's entity names."""

    NAMES = {"sensor.givtcp_ab12345678_soc_kwh", "sensor.givtcp_ab12345678_battery_power", "sensor.predbat_givtcp_0_battery_power", "sensor.predbat_givtcp_0_load_today"}
    ARGS = {"soc_kw": ["sensor.givtcp_{geserial}_soc_kwh"], "battery_power": ["sensor.givtcp_{geserial}_battery_power"]}

    def test_template_values_finds_the_serial(self):
        """The value a placeholder stands for is read off the matching entity name."""
        self.assertEqual(fixture_db.template_values(self.ARGS, self.NAMES), {"ab12345678"})

    def test_build_remap_resolves_templates_and_prefers_the_predbat_inverter_entity(self):
        """A templated setting resolves to the real entity; Predbat's own inverter entity wins when it exists, even for unset settings."""
        remap = {r.dummy_entity: r.real_entity for r in fixture_db.build_remap(self.ARGS, "predbat", self.NAMES)}
        self.assertEqual(remap["sensor.predbat_dummy_battery_soc"], "sensor.givtcp_ab12345678_soc_kwh")
        self.assertEqual(remap["sensor.predbat_dummy_battery_power"], "sensor.predbat_givtcp_0_battery_power")
        self.assertEqual(remap["sensor.predbat_dummy_load_lifetime"], "sensor.predbat_givtcp_0_load_today")
        self.assertNotIn("sensor.predbat_dummy_pv_lifetime", remap)

    def test_build_remap_skips_a_template_with_no_matching_entity(self):
        """A templated setting that matches nothing in the database is not remapped."""
        self.assertEqual(fixture_db.build_remap({"soc_kw": ["sensor.givtcp_{geserial}_soc_kwh"]}, "predbat", {"sensor.other"}), [])


class TestExportDb(unittest.TestCase):
    """export_db writes a database the benchmark's Predbat can run from."""

    def setUp(self):
        """Build a synthetic live database and an empty output directory."""
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "live").mkdir()
        (root / "out").mkdir()
        self.live = make_live_db(root / "live")
        self.out = root / "out" / "predbat.db"
        self.remap = fixture_db.build_remap(LIVE_ARGS, "predbat")

    def tearDown(self):
        """Remove the temporary directory."""
        self.tmp.cleanup()

    def export(self):
        """Run the export with the test's settings and return its stats."""
        return fixture_db.export_db(self.live, self.out, T0, 7, self.remap, "predbat", dropped=lambda name: False, replacements={"A-1234": "A-FAKE"})

    def test_export_db_schema_matches_database_engine(self):
        """The output has exactly the tables and indexes DatabaseEngine creates."""
        self.export()
        reference = Path(self.tmp.name) / "reference"
        reference.mkdir()
        DatabaseEngine(StubBase(reference), 30)._close()
        sql = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        self.assertEqual(query(self.out, sql), query(reference / "predbat.db", sql))

    def test_export_db_window_remap_and_drop(self):
        """Old rows are cut, inverter history is re-keyed and converted, and Predbat's own entities stay by default."""
        stats = self.export()
        load = history(self.out, "sensor.predbat_dummy_load_lifetime")
        self.assertEqual([float(state) for _, state in load], [0.0, 1.0, 1.0, 1.5])
        self.assertEqual(stats["totals"]["load"], 1.5)
        self.assertEqual(history(self.out, "sensor.real_load_today"), [])
        self.assertEqual([state for _, state in history(self.out, "sensor.predbat_dummy_battery_power")], ["-1500.0"])
        self.assertEqual([state for _, state in history(self.out, "sensor.predbat_dummy_battery_capacity")], ["9.5"])
        self.assertEqual([state for _, state in history(self.out, "predbat.plan_history")], ["x"])
        self.assertEqual([state for _, state in history(self.out, "sensor.predbat_cost_today")], ["1.23"])
        self.assertEqual([state for _, state in history(self.out, "switch.predbat_expert_mode")], ["on"])
        latest = query(self.out, "SELECT l.attributes FROM latest l JOIN entities e USING(entity_index) WHERE e.entity_name = 'sensor.octopus_rates'")
        self.assertEqual(json.loads(latest[0][0]), {"account": "A-FAKE"})
        oldest = query(self.out, "SELECT min(datetime) FROM states")[0][0]
        self.assertGreaterEqual(datetime.strptime(oldest, TIME_FORMAT_DB), T0 - timedelta(days=7))
        self.assertEqual(stats["dropped"], 0)

    def test_export_db_strip_outputs_drops_predbat_outputs_and_keeps_settings(self):
        """With strip_outputs, Predbat's own outputs are left out and its settings kept."""
        stats = fixture_db.export_db(self.live, self.out, T0, 7, self.remap, "predbat", dropped=lambda name: False, replacements={}, strip_outputs=True)
        self.assertEqual(history(self.out, "predbat.plan_history"), [])
        self.assertEqual(history(self.out, "sensor.predbat_cost_today"), [])
        self.assertEqual([state for _, state in history(self.out, "switch.predbat_expert_mode")], ["on"])
        self.assertEqual(stats["dropped"], 2)

    def test_export_db_keeps_a_remapped_value_that_last_changed_before_the_window(self):
        """A sensor recorded only on change (battery capacity) keeps its latest value even when it is older than the window."""
        engine = DatabaseEngine(StubBase(self.live.parent), 30)
        engine._set_state_db("sensor.old_soc", "4.2", {"unit_of_measurement": "kWh"}, T0 - timedelta(days=20))
        engine._close()
        remap = [fixture_db.Remap("sensor.old_soc", "sensor.predbat_dummy_battery_soc", "gauge_kwh")]
        fixture_db.export_db(self.live, self.out, T0, 7, remap, "predbat", dropped=lambda name: False, replacements={})
        latest = query(self.out, "SELECT l.state FROM latest l JOIN entities e USING(entity_index) WHERE e.entity_name = 'sensor.predbat_dummy_battery_soc'")
        self.assertEqual(latest, [("4.2",)])

    def test_export_db_carries_in_the_state_at_window_start(self):
        """An entity that last changed before the window keeps that state as one row at the window's start, as a history query sees it."""
        engine = DatabaseEngine(StubBase(self.live.parent), 30)
        engine._set_state_db("sensor.rarely_changes", "12.5", {"unit_of_measurement": "kWh"}, T0 - timedelta(days=9))
        engine._set_state_db("sensor.rarely_changes", "13.0", {"unit_of_measurement": "kWh"}, T0 - timedelta(days=8))
        engine._close()
        self.export()
        self.assertEqual(history(self.out, "sensor.rarely_changes"), [((T0 - timedelta(days=7)).strftime(TIME_FORMAT_DB), "13.0")])

    def test_octopus_account_ids_are_read_off_entity_names(self):
        """The Octopus component names its entities after the account number, which the export scrubs."""
        names = {"binary_sensor.predbat_octopus_a_1234abcd_saving_session", "sensor.predbat_octopus_a_1234abcd_free_electricity", "sensor.other"}
        self.assertEqual(fixture_db.octopus_account_ids(names, "predbat"), {"1234abcd"})

    def test_export_db_dropped_predicate_removes_entities(self):
        """An entity the caller marks as dropped is not exported."""
        fixture_db.export_db(self.live, self.out, T0, 7, self.remap, "predbat", dropped=lambda name: name == "sensor.octopus_rates", replacements={})
        self.assertEqual(history(self.out, "sensor.octopus_rates"), [])

    def test_snapshot_db_copies_the_live_database(self):
        """A snapshot holds the same entities and rows as the live database, which is left unchanged."""
        before = (self.live.read_bytes(), os.stat(self.live).st_mtime_ns)
        copy = Path(self.tmp.name) / "snapshot.db"
        fixture_db.snapshot_db(self.live, copy)
        sql = "SELECT e.entity_name, s.datetime, s.state FROM states s JOIN entities e USING(entity_index) ORDER BY 1, 2"
        self.assertEqual(query(copy, sql), query(self.live, sql))
        self.assertEqual((self.live.read_bytes(), os.stat(self.live).st_mtime_ns), before)

    def test_snapshot_db_retries_while_the_live_database_is_being_written(self):
        """A read that collides with the live writer is retried, and gives up with a clear error after the last attempt."""
        copy = Path(self.tmp.name) / "snapshot.db"
        real = fixture_db._backup_once
        calls = []

        def flaky(source, dest):
            """Fail like a hot journal twice, then copy."""
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return real(source, dest)

        with mock.patch.object(fixture_db, "_backup_once", flaky), mock.patch.object(fixture_db.time, "sleep"):
            fixture_db.snapshot_db(self.live, copy, attempts=3)
        self.assertEqual(len(calls), 3)
        with mock.patch.object(fixture_db, "_backup_once", side_effect=sqlite3.OperationalError("busy")), mock.patch.object(fixture_db.time, "sleep"):
            with self.assertRaises(RuntimeError):
                fixture_db.snapshot_db(self.live, copy, attempts=2)

    def test_export_db_opens_live_db_read_only(self):
        """The live database's bytes and modification time are unchanged by an export."""
        before = (self.live.read_bytes(), os.stat(self.live).st_mtime_ns)
        self.export()
        self.assertEqual((self.live.read_bytes(), os.stat(self.live).st_mtime_ns), before)


class TestPersonalAttributes(unittest.TestCase):
    """export_db scrubs personal attributes in every exported row."""

    def test_export_db_merges_entities_that_scrub_to_the_same_name(self):
        """Two entities whose names differ only in a serial's case become one, rather than failing the export."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = DatabaseEngine(StubBase(root), 30)
            engine._set_state_db("sensor.solax_AB12345678_soc", "40", {}, T0 - timedelta(hours=2))
            engine._set_state_db("sensor.solax_ab12345678_soc", "41", {}, T0 - timedelta(hours=1))
            engine._close()
            out = root / "out.db"
            stats = fixture_db.export_db(root / "predbat.db", out, T0, 7, [], "predbat", dropped=lambda name: False, replacements={}, tokens={"ab12345678": "benchid1"})
            self.assertEqual(query(out, "SELECT entity_name FROM entities WHERE entity_name LIKE 'sensor.solax_%'"), [("sensor.solax_benchid1_soc",)])
            self.assertEqual(stats["merged"], 1)

    def test_export_db_scrubs_coordinates_in_history_and_latest(self):
        """zone.home's real coordinates are the fake ones in both its history and its latest state."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "live").mkdir()
            live = make_live_db(root / "live")
            out = root / "predbat.db"
            fixture_db.export_db(live, out, T0, 7, [], "predbat", dropped=lambda name: False, replacements={})
            rows = query(out, "SELECT s.attributes FROM states s JOIN entities e USING(entity_index) WHERE e.entity_name = 'zone.home' UNION ALL SELECT l.attributes FROM latest l JOIN entities e USING(entity_index) WHERE e.entity_name = 'zone.home'")
            self.assertEqual(len(rows), 2)
            for (attributes,) in rows:
                self.assertEqual((json.loads(attributes)["latitude"], json.loads(attributes)["longitude"]), (51.501, -0.1419))


if __name__ == "__main__":
    unittest.main()
