# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the memory benchmark's export CLI and generated apps.yaml, on synthetic live inputs."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "apps" / "predbat"))

import export_fixture  # noqa: E402
import fixture_cache  # noqa: E402
import scrub  # noqa: E402
from db_engine import DatabaseEngine  # noqa: E402

T0 = datetime(2026, 11, 26, 8, 0, 0, tzinfo=timezone.utc)

LIVE_APPS_TEXT = """
pred_bat:
  module: predbat
  class: PredBat
  prefix: predbat
  timezone: Europe/Paris
  ha_url: 'http://homeassistant.local:8123'
  ha_key: !secret ha_key
  geserial: 'auto'
  carbon_postcode: 'QQ1 1QQ'
  mcp_enable: true
  load_ml_enable: true
  threads: auto
  givtcp_rest:
    - 'http://inverter.local:6345'
  soc_max:
    - sensor.real_capacity
  soc_kw:
    - sensor.givtcp_{geserial}_soc_kwh
  load_today:
    - sensor.real_load_today
  charge_rate:
    - number.real_charge_rate
  car_charging_now: sensor.car_plugged
  days_previous: 7
  watch_list:
    - sensor.givtcp_{geserial}_load_energy_today_kwh_prediction
  octopus_api_key: !secret octopus_key
  octopus_api_account: !secret octopus_account
  wallbox_username: !secret wallbox_user
  chat:
    provider: something
  forecast_solar:
    - postcode: ZZ9 9ZZ
      kwp: 2.0
      azimuth: 90
      declination: 30
      api_key: real-forecast-key
      efficiency: 0.9
"""


class StubBase:
    """Just enough of PredBat for DatabaseEngine to open a database."""

    def __init__(self, root):
        self.config_root = str(root)
        self.now_utc_real = T0.replace(tzinfo=None)
        self.log = lambda *args, **kwargs: None


def make_live(root, plant_leak=False):
    """Write a synthetic live add-on config directory: apps.yaml, predbat.db and cache/."""
    live = Path(root) / "live"
    (live / "cache").mkdir(parents=True)
    (live / "apps.yaml").write_text(LIVE_APPS_TEXT)
    engine = DatabaseEngine(StubBase(live), 30)
    when = T0.replace(tzinfo=None) - timedelta(hours=2)
    engine._set_state_db("sensor.real_capacity", "9.5", {"unit_of_measurement": "kWh"}, when)
    engine._set_state_db("sensor.givtcp_xx1234y567_soc_kwh", "4.75", {"unit_of_measurement": "kWh"}, when)
    engine._set_state_db("sensor.real_load_today", "3.0", {"unit_of_measurement": "kWh"}, when)
    engine._set_state_db("sensor.car_plugged", "on", {"serial": "XX1234Y567"}, when)
    engine._set_state_db("binary_sensor.predbat_octopus_a_1234abcd_saving_session", "off", {"account": "A-1234ABCD"}, when)
    engine._set_state_db("event.octopus_energy_electricity_21e1234567_rates", "on", {}, when)
    engine._set_state_db("sensor.predbat_solax_1234567890123456789_soc", "50", {}, when)
    engine._set_state_db("zone.home", "0", {"latitude": 52.987654, "longitude": -1.234567}, when)
    engine._set_state_db("sensor.weather", "sunny", {"note": "QQ1 1QQ" if plant_leak else "fine"}, when)
    engine._close()
    source = fixture_cache.solcast_source()
    config = yaml.safe_load(LIVE_APPS_TEXT.replace("!secret ", ""))["pred_bat"]["forecast_solar"]
    lookup = {"status": 200, "result": {"latitude": 52.1, "longitude": -1.6}}
    fixture_cache.write_entry(live / "cache", "solar", source.cache_key(fixture_cache.POSTCODE_URL.format("ZZ9 9ZZ"), {}), lookup, "json", T0)
    url, params = fixture_cache.forecast_requests(config, {"ZZ9 9ZZ": (52.1, -1.6)}, source)[0]
    forecast = {"result": {"watts": {"2026-11-26 09:00:00": 100}}, "message": {"info": {"latitude": 52.1, "longitude": -1.6, "place": "Real Place"}}}
    fixture_cache.write_entry(live / "cache", "solar", source.cache_key(url, params), forecast, "json", T0)
    account = {"account_data": {"account": {"electricityAgreements": [{"meterPoint": {"mpan": "1900000000001", "meters": [{"serialNumber": "21E1234567"}]}}]}}, "kraken_token": "eyJ0eXAiOiJKV1QifQ.test.signature"}
    fixture_cache.write_entry(live / "cache", "octopus_user", "account", account, "yaml", T0)
    return live


class TestBuildAppsYaml(unittest.TestCase):
    """build_apps_yaml turns the live apps.yaml into the benchmark's, with nothing real or networked left in it."""

    def setUp(self):
        """Load the synthetic live apps.yaml and the committed template."""
        self.live_args = export_fixture.load_live_apps_text(LIVE_APPS_TEXT)
        self.template = export_fixture.load_template()
        self.neutral = [{"postcode": scrub.FAKE_POSTCODE, "kwp": 2.0, "api_key": scrub.FAKE_FORECAST_KEY}]
        self.totals = {"pv": 1.0, "load": 2.0, "import": 0.0, "export": 0.0}
        self.sizing = {"battery_size": 9.5, "soc_initial": 50.0}

    def build(self):
        """Build the generated apps.yaml's pred_bat block."""
        return export_fixture.build_apps_yaml(self.template, self.live_args, self.neutral, self.totals, self.sizing)["pred_bat"]

    def test_build_apps_yaml_has_no_real_identifiers(self):
        """Secrets, the HA url, the inverter serial and dropped components' settings are gone; kept settings stay."""
        apps = self.build()
        text = yaml.safe_dump(apps)
        for real in ("XX1234Y567", "QQ1 1QQ", "homeassistant.local", "real-forecast-key", "ZZ9 9ZZ", "inverter.local"):
            self.assertNotIn(real, text)
        for key in ("ha_key", "ha_url", "wallbox_username", "chat", "givtcp_rest", "carbon_postcode", "geserial", "charge_rate", "soc_max", "load_today", "watch_list"):
            self.assertNotIn(key, apps)
        self.assertEqual(apps["car_charging_now"], "sensor.car_plugged")
        self.assertEqual(apps["days_previous"], 7)
        self.assertEqual(apps["timezone"], "Europe/Paris")
        self.assertEqual((apps["octopus_api_key"], apps["octopus_api_account"]), (scrub.FAKE_OCTOPUS_KEY, scrub.FAKE_ACCOUNT))
        self.assertEqual(apps["forecast_solar"], self.neutral)

    def test_build_apps_yaml_disables_mcp_load_ml_and_givtcp(self):
        """MCP is left out (the component treats the key's presence as on), Load ML is off, threads fixed at 0, the database is primary and there is no HA connection."""
        apps = self.build()
        self.assertNotIn("mcp_enable", apps)
        self.assertIs(apps["load_ml_enable"], False)
        self.assertEqual(apps["threads"], 0)
        self.assertIs(apps["db_primary"], True)
        self.assertIs(apps["db_enable"], True)
        self.assertNotIn("givtcp_rest", apps)

    def test_build_apps_yaml_dummy_block_uses_live_sizing_and_totals(self):
        """The dummy inverter is sized from the live battery and its lifetime counters start at the history's totals."""
        dummy = self.build()["dummy_inverter"]
        self.assertEqual(dummy["battery_size"], 9.5)
        self.assertEqual(dummy["soc_initial"], 50.0)
        self.assertEqual(dummy["totals"], self.totals)


class TestExportFixtureCli(unittest.TestCase):
    """The CLI builds the whole fixture, and refuses to leave one behind that leaks a real value."""

    def test_export_fixture_cli_end_to_end_on_synthetic_inputs(self):
        """A clean live directory gives the five outputs, a parseable T0 and the dummy sized from the database."""
        with tempfile.TemporaryDirectory() as tmp:
            live = make_live(tmp)
            out = Path(tmp) / "fixture"
            self.assertEqual(export_fixture.main(["--live", str(live), "--out", str(out), "--days", "7", "--t0", T0.isoformat()]), 0)
            for name in ("predbat.db", "cache", "apps.yaml", "manifest.json"):
                self.assertTrue((out / name).exists(), name)
            manifest = json.loads((out / "manifest.json").read_text())
            datetime.fromisoformat(manifest["t0"])
            self.assertEqual(manifest["days"], 7)
            apps = yaml.safe_load((out / "apps.yaml").read_text())["pred_bat"]
            self.assertEqual(apps["dummy_inverter"]["battery_size"], 9.5)
            self.assertEqual(apps["dummy_inverter"]["soc_initial"], 50.0)
            self.assertEqual(scrub.find_leaks(out, ["A-9876", "1900000000001", "XX1234Y567", "xx1234y567", "real-forecast-key", "Real Place", "a_1234abcd", "A-1234ABCD", "21e1234567", "1234567890123456789", "52.987654"]), [])
            self.assertEqual(scrub.find_personal_data(out), [])
            self.assertIn(b"binary_sensor.predbat_octopus_a_bench0001_saving_session", (out / "predbat.db").read_bytes())

    def test_export_fixture_cli_refuses_a_leak_and_removes_its_output(self):
        """A real value the scrub does not cover makes the CLI fail and leaves no fixture behind."""
        with tempfile.TemporaryDirectory() as tmp:
            live = make_live(tmp, plant_leak=True)
            out = Path(tmp) / "fixture"
            self.assertNotEqual(export_fixture.main(["--live", str(live), "--out", str(out), "--t0", T0.isoformat()]), 0)
            self.assertFalse(out.exists())
            self.assertFalse(Path(str(out) + ".tmp").exists())

    def test_export_fixture_cli_will_not_replace_a_directory_it_did_not_write(self):
        """An existing output directory without a manifest is left alone and the CLI fails."""
        with tempfile.TemporaryDirectory() as tmp:
            live = make_live(tmp)
            out = Path(tmp) / "fixture"
            out.mkdir()
            (out / "precious.txt").write_text("keep me")
            self.assertNotEqual(export_fixture.main(["--live", str(live), "--out", str(out), "--t0", T0.isoformat()]), 0)
            self.assertEqual((out / "precious.txt").read_text(), "keep me")


class TestExportPersonalDataCheck(unittest.TestCase):
    """The export refuses to leave a fixture that still looks like it holds personal data."""

    def test_export_fixture_cli_refuses_an_email_it_cannot_scrub(self):
        """An email inside a free-text attribute is found by its shape, and the export fails and removes its output."""
        with tempfile.TemporaryDirectory() as tmp:
            live = make_live(tmp)
            engine = DatabaseEngine(StubBase(live), 30)
            engine._set_state_db("sensor.note", "x", {"note": "contact jo@example.org"}, T0.replace(tzinfo=None) - timedelta(hours=1))
            engine._close()
            out = Path(tmp) / "fixture"
            self.assertNotEqual(export_fixture.main(["--live", str(live), "--out", str(out), "--t0", T0.isoformat()]), 0)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
