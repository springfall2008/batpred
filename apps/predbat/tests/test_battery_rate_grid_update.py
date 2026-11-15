# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the rate grid wiring: battery_rate_grid_update, its storage, the fallback to manual curves and the sensors."""

import shutil
import tempfile
from datetime import timedelta

from tests.test_infra import TestHAInterface, MockStorageComponents, make_test_storage, run_async
from predbat import PredBat
from battery_curve import charge_day
from battery_rate_grid import rate_grid_curve
from config import APPS_SCHEMA
from const import BATTERY_RATE_GRID_SCAN_MINUTES


class FakeInverter:
    """An inverter whose rate grid scan returns the samples a test sets (None means the history was missing)."""

    def __init__(self):
        """Nothing scanned yet."""
        self.scan = {False: {}, True: {}}
        self.calls = {False: 0, True: 0}
        self.curve_scan_coverage = {}

    def find_rate_grid_samples(self, discharge):
        """The samples set for this direction."""
        self.calls[discharge] += 1
        value = self.scan[discharge]
        return None if value is None else {day: [list(entry) for entry in entries] for day, entries in value.items()}


class FlakyStorage:
    """A storage whose first `fails` reads come back as None, as a failed KeyDB or file read does."""

    def __init__(self, storage, fails):
        """Wrap a real storage."""
        self.storage = storage
        self.fails = fails

    async def load(self, module, filename):
        """None for the first reads, then the real data."""
        if self.fails > 0:
            self.fails -= 1
            return None
        return await self.storage.load(module, filename)

    async def save(self, module, filename, data, format="yaml"):
        """Saves go through."""
        return await self.storage.save(module, filename, data, format=format)


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def _make(args=None):
    """A PredBat with a 10 kWh, 2.6 kW battery and the given apps.yaml settings applied."""
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.auto_config()
    my_predbat.load_user_config()
    for key in ("battery_charge_power_curve", "battery_discharge_power_curve", "battery_temperature_charge_curve", "battery_temperature_discharge_curve"):
        my_predbat.args.pop(key, None)  # the test apps.yaml sets a charge curve
    for key, value in (args or {}).items():
        my_predbat.args[key] = value
    my_predbat.fetch_config_options()
    my_predbat.soc_max = 10.0
    my_predbat.battery_rate_max_charge = 2600 / 60000
    my_predbat.battery_rate_max_discharge = 2600 / 60000
    return my_predbat


def _samples(my_predbat, days=4, temps=(10, 20), knee=70):
    """Learnable segments: full rate below the knee SoC, 0.3 above, over `days` evidence days."""
    samples = {}
    for d in range(days):
        day = charge_day(my_predbat.now_utc, d * 24 * 60 + 60, my_predbat.local_tz)
        rows = samples.setdefault(day, [])
        for temp in temps:
            for soc in range(10, 100, 4):
                rows.append([soc + 0.4, temp + 0.3, 1.0 if soc < knee else 0.3])
    return samples


def _sensor(my_predbat, kind, attribute=None):
    """The published binary sensor for a rate grid, or one of its attributes."""
    my_predbat.publish_battery_rate_grid_sensors()
    return my_predbat.get_state_wrapper("binary_sensor.{}_battery_{}_rate_grid".format(my_predbat.prefix, kind), attribute=attribute)


def _update(my_predbat, inv):
    """Run both grid updates."""
    my_predbat.battery_rate_grid_update(inv, False)
    my_predbat.battery_rate_grid_update(inv, True)


def run_battery_rate_grid_update_tests(my_predbat_unused=None):
    """Run the rate grid wiring tests."""
    failed = False
    tmpdir = tempfile.mkdtemp()
    try:
        print("Test 1: no curves and no evidence - the default grid (full rate, 1C capped)")
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t1")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        _update(my_predbat, inv)
        failed |= _check("t1 source", my_predbat.battery_rate_grid_source.get("charge") == "default", my_predbat.battery_rate_grid_source)
        failed |= _check("t1 grid", rate_grid_curve(my_predbat.battery_charge_rate_grid, 10) == (1.0,) * 101)
        failed |= _check("t1 sensor off", _sensor(my_predbat, "charge") == "off")

        print("Test 2: a manual SoC curve is the fallback; 'auto' means no manual curve")
        my_predbat = _make({"battery_charge_power_curve": {100: 0.5}})
        my_predbat.components = MockStorageComponents(make_test_storage(my_predbat, tmpdir + "/t2"))
        _update(my_predbat, FakeInverter())
        failed |= _check("t2 source", my_predbat.battery_rate_grid_source.get("charge") == "manual", my_predbat.battery_rate_grid_source)
        failed |= _check("t2 grid", rate_grid_curve(my_predbat.battery_charge_rate_grid, 10)[100] == 0.5)
        my_predbat = _make({"battery_charge_power_curve": "auto"})
        my_predbat.components = MockStorageComponents(make_test_storage(my_predbat, tmpdir + "/t2b"))
        _update(my_predbat, FakeInverter())
        failed |= _check("t2 auto is unset", my_predbat.battery_rate_grid_source.get("charge") == "default", my_predbat.battery_rate_grid_source)

        print("Test 3: enough evidence learns the grid, on by default, and the sensor shows it")
        my_predbat = _make({"battery_charge_power_curve": {100: 0.5}})
        storage = make_test_storage(my_predbat, tmpdir + "/t3")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        _update(my_predbat, inv)
        failed |= _check("t3 auto on by default", my_predbat.battery_rate_grid_auto is True)
        failed |= _check("t3 source", my_predbat.battery_rate_grid_source.get("charge") == "learned", my_predbat.battery_rate_grid_source)
        row = rate_grid_curve(my_predbat.battery_charge_rate_grid, 15)
        failed |= _check("t3 learned taper", row[90] < 0.5 and row[30] > 0.95, (row[30], row[90]))
        failed |= _check("t3 sensor on", _sensor(my_predbat, "charge") == "on")
        for attribute in ("source", "cells", "days", "temperature_range", "updated", "last_scan", "table"):
            failed |= _check("t3 attribute " + attribute, _sensor(my_predbat, "charge", attribute) is not None, attribute)
        failed |= _check("t3 discharge has no evidence", my_predbat.battery_rate_grid_source.get("discharge") == "default", my_predbat.battery_rate_grid_source)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t3 saved", isinstance(saved, dict) and len(saved.get("samples", {})) == 4 and saved.get("updated"), saved and list(saved.keys()))

        print("Test 4: a second call within the scan interval does not rescan; after a restart the stored evidence is used")
        calls = inv.calls[False]
        _update(my_predbat, inv)
        failed |= _check("t4 no rescan", inv.calls[False] == calls, inv.calls)
        my_predbat.now_utc += timedelta(minutes=BATTERY_RATE_GRID_SCAN_MINUTES + 1)
        _update(my_predbat, inv)
        failed |= _check("t4 rescan when due", inv.calls[False] == calls + 1, inv.calls)
        restarted = _make({"battery_charge_power_curve": {100: 0.5}})
        restarted.components = MockStorageComponents(storage)
        inv2 = FakeInverter()
        inv2.scan[False] = None
        _update(restarted, inv2)
        failed |= _check("t4 restored", restarted.battery_rate_grid_source.get("charge") == "learned", restarted.battery_rate_grid_source)

        print("Test 5: auto off - the manual curve is used, the evidence is still gathered and stored")
        my_predbat = _make({"battery_charge_power_curve": {100: 0.5}, "battery_rate_grid_auto": False})
        storage = make_test_storage(my_predbat, tmpdir + "/t5")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        _update(my_predbat, inv)
        failed |= _check("t5 source", my_predbat.battery_rate_grid_source.get("charge") == "manual", my_predbat.battery_rate_grid_source)
        failed |= _check("t5 grid", rate_grid_curve(my_predbat.battery_charge_rate_grid, 15)[100] == 0.5)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t5 stored", isinstance(saved, dict) and len(saved.get("samples", {})) == 4)

        print("Test 6: a failed first read does not lose the stored year")
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t6")
        old_day = charge_day(my_predbat.now_utc, 200 * 24 * 60, my_predbat.local_tz)
        run_async(storage.save("predbat", "battery_rate_grid_charge", {"samples": {old_day: [[50.0, 12.0, 0.4]]}, "updated": None}, format="json"))
        my_predbat.components = MockStorageComponents(FlakyStorage(storage, 1))
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        _update(my_predbat, inv)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t6 old day kept", isinstance(saved, dict) and old_day in saved.get("samples", {}), saved and sorted(saved.get("samples", {}).keys()))

        print("Test 7: the options")
        failed |= _check("t7 new option", "battery_rate_grid_auto" in APPS_SCHEMA)
        failed |= _check("t7 removed option", "battery_temperature_charge_curve_auto" not in APPS_SCHEMA)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failed
