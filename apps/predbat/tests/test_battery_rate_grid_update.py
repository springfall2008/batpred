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
import yaml

from battery_rate_grid import rate_grid_curve
from utils import rate_curve_function
from config import APPS_SCHEMA
from const import BATTERY_RATE_GRID_MAX_AGE_DAYS, BATTERY_RATE_GRID_RESAVE_MINUTES, BATTERY_RATE_GRID_SCAN_MINUTES, TIME_FORMAT


class FakeInverter:
    """An inverter whose rate grid scan returns the samples a test sets (None means the history was missing)."""

    def __init__(self):
        """Nothing scanned yet."""
        self.scan = {False: {}, True: {}}
        self.calls = {False: 0, True: 0}
        self.days = {}
        self.curve_scan_coverage = {}

    def find_rate_grid_samples(self, discharge, days=None):
        """The samples set for this direction."""
        self.calls[discharge] += 1
        self.days.setdefault(discharge, []).append(days)
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

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Saves go through."""
        return await self.storage.save(module, filename, data, format=format, expiry=expiry)


class RecordingStorage(FlakyStorage):
    """A storage that never fails and records the expiry each save asked for."""

    def __init__(self, storage):
        """Wrap a real storage."""
        super().__init__(storage, 0)
        self.expiries = {}

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record the expiry, then save."""
        self.expiries[filename] = expiry
        return await super().save(module, filename, data, format=format, expiry=expiry)


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


def _settle(my_predbat, inv, cycles=3, minutes=6):
    """
    Run the updates over several cycles a few minutes apart: an empty store reads back as None, like a failed read,
    so a new install's first save waits until the reads have failed on separate cycles for long enough.
    """
    for _ in range(cycles):
        _update(my_predbat, inv)
        my_predbat.now_utc += timedelta(minutes=minutes)


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
        failed |= _check("t1 grid", rate_grid_curve(my_predbat.battery_charge_rate_grid, 10)[:101] == (1.0,) * 101)
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
        _settle(my_predbat, inv)
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

        print("Test 3b: the evidence is saved with an expiry past its retention, so a backend's default lifetime cannot drop it (#6225)")
        my_predbat = _make({"battery_charge_power_curve": {100: 0.5}})
        recording = RecordingStorage(make_test_storage(my_predbat, tmpdir + "/t3b"))
        my_predbat.components = MockStorageComponents(recording)
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        _settle(my_predbat, inv)
        expiry = recording.expiries.get("battery_rate_grid_charge")
        failed |= _check("t3b saved", "battery_rate_grid_charge" in recording.expiries, recording.expiries)
        failed |= _check("t3b expiry past retention", expiry is not None and expiry >= my_predbat.now_utc + timedelta(days=BATTERY_RATE_GRID_MAX_AGE_DAYS), expiry)
        failed |= _check("t3b reads back", isinstance(run_async(recording.load("predbat", "battery_rate_grid_charge")), dict))

        print("Test 3c: unchanged evidence is still re-saved once after a restart and then daily, so its expiry keeps moving (#6225)")
        restarted = _make({"battery_charge_power_curve": {100: 0.5}})
        resaves = RecordingStorage(recording.storage)
        restarted.components = MockStorageComponents(resaves)
        quiet = FakeInverter()
        quiet.scan[False] = None  # no new evidence: the scan leaves the stored samples alone
        _update(restarted, quiet)
        failed |= _check("t3c saved after the first load", "battery_rate_grid_charge" in resaves.expiries, resaves.expiries)
        resaves.expiries.clear()
        # Within the hour no scan (and so no re-fit) is due: nothing changed, so nothing is saved
        restarted.now_utc += timedelta(minutes=30)
        _update(restarted, quiet)
        failed |= _check("t3c not again within a day", "battery_rate_grid_charge" not in resaves.expiries, resaves.expiries)
        # A day after the last save it is saved again although still nothing has changed
        restarted.battery_rate_grid_last_save["charge"] = restarted.now_utc - timedelta(minutes=BATTERY_RATE_GRID_RESAVE_MINUTES)
        restarted.now_utc += timedelta(minutes=1)
        _update(restarted, quiet)
        expiry = resaves.expiries.get("battery_rate_grid_charge")
        failed |= _check("t3c again a day later", expiry is not None and expiry >= restarted.now_utc + timedelta(days=BATTERY_RATE_GRID_MAX_AGE_DAYS), expiry)

        print("Test 4: a second call within the scan interval does not rescan; after a restart the stored evidence is used")
        calls = inv.calls[False]
        failed |= _check("t4 one scan while settling", calls == 1, inv.calls)
        _update(my_predbat, inv)
        failed |= _check("t4 no rescan", inv.calls[False] == calls, inv.calls)
        my_predbat.now_utc += timedelta(minutes=BATTERY_RATE_GRID_SCAN_MINUTES + 1)
        _update(my_predbat, inv)
        failed |= _check("t4 rescan when due", inv.calls[False] == calls + 1, inv.calls)
        failed |= _check("t4 first scan reads up to 28 days, daily scans 2", inv.days[False] == [28, 2], inv.days)
        restarted = _make({"battery_charge_power_curve": {100: 0.5}})
        restarted.components = MockStorageComponents(storage)
        inv2 = FakeInverter()
        inv2.scan[False] = None
        _update(restarted, inv2)
        failed |= _check("t4 restored", restarted.battery_rate_grid_source.get("charge") == "learned", restarted.battery_rate_grid_source)

        print("Test 4b: after a restart the first scan reads only the days since the stored last success (#6287)")
        my_predbat = _make({"battery_charge_power_curve": {100: 0.5}})
        storage = make_test_storage(my_predbat, tmpdir + "/t4b")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        _settle(my_predbat, inv)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t4b last success saved", isinstance(saved, dict) and isinstance(saved.get("last_success"), str) and saved.get("scan_key") == my_predbat.rate_grid_scan_key(False), saved and {k: saved.get(k) for k in ("last_success", "scan_key")})

        restarts = [0]

        def restart(hours_later, setup=None, payload=None):
            """
            A new instance `hours_later` after the original's clock, on its own copy of the saved store (or of `payload`), so
            one case's save cannot set up the next; returns the days its first scan read and the instance.
            """
            restarts[0] += 1
            store = make_test_storage(my_predbat, tmpdir + "/t4b_{}".format(restarts[0]))
            run_async(store.save("predbat", "battery_rate_grid_charge", payload or saved, format="json"))
            restarted = _make({"battery_charge_power_curve": {100: 0.5}})
            restarted.now_utc = my_predbat.now_utc + timedelta(hours=hours_later)
            if setup:
                setup(restarted)
            restarted.components = MockStorageComponents(store)
            scanner = FakeInverter()
            scanner.scan[False] = {}
            restarted.battery_rate_grid_update(scanner, False)
            return scanner.days.get(False), restarted

        days, restarted = restart(1)
        failed |= _check("t4b restart an hour later reads 2 days", days == [2], days)
        failed |= _check("t4b restart keeps the learned grid", restarted.battery_rate_grid_source.get("charge") == "learned", restarted.battery_rate_grid_source)
        days, _ = restart(4 * 24)
        failed |= _check("t4b restart four days later reads 5 days", days == [5], days)

        def bigger_battery(restarted):
            """The battery size changed, so the stored samples were measured against another size."""
            restarted.soc_max = 12.0

        def faster_rate(restarted):
            """The rated charge rate changed."""
            restarted.battery_rate_max_charge = 3600 / 60000

        days, _ = restart(1, bigger_battery)
        failed |= _check("t4b battery size change reads the full window", days == [28], days)
        days, _ = restart(1, faster_rate)
        failed |= _check("t4b rated rate change reads the full window", days == [28], days)

        def deeper_history(restarted):
            """battery_rate_grid_history_days raised, so the deeper window is read once."""
            restarted.battery_rate_grid_history_days = 60

        days, _ = restart(1, deeper_history)
        failed |= _check("t4b history days change reads the full window", days == [60], days)

        # A store written before last_success was saved (or with a time ahead of the clock) gets the full window, as before
        days, _ = restart(1, payload={"samples": saved["samples"], "updated": saved["updated"]})
        failed |= _check("t4b store without last_success reads the full window", days == [28], days)
        days, _ = restart(-1)
        failed |= _check("t4b last success ahead of the clock reads the full window", days == [28], days)

        def lossier_battery(restarted):
            """battery_loss changed."""
            restarted.battery_loss = restarted.battery_loss - 0.05

        def drifted_size(restarted):
            """battery_scaling_auto or SoH moved the size by 1%, within the tolerance."""
            restarted.soc_max = restarted.soc_max * 1.01

        def samples_only_in_memory(restarted):
            """Samples already held in memory, but none in the store the last success came from."""
            restarted.battery_rate_grid_samples["charge"] = saved["samples"]

        days, _ = restart(1, lossier_battery)
        failed |= _check("t4b battery_loss change reads the full window", days == [28], days)
        days, _ = restart(1, drifted_size)
        failed |= _check("t4b battery size drift within the tolerance reads 2 days", days == [2], days)
        days, _ = restart(1, payload=dict(saved, samples={}))
        failed |= _check("t4b a store whose scans found no evidence yet reads 2 days", days == [2], days)
        day = sorted(saved["samples"])[0]
        days, _ = restart(1, samples_only_in_memory, payload=dict(saved, samples={day: "not a sample list"}))
        failed |= _check("t4b a store that lost samples to cleaning reads the full window", days == [28], days)
        damaged_rows = {stored_day: list(entries) for stored_day, entries in saved["samples"].items()}
        damaged_rows[day] = damaged_rows[day] + [["bad"]]
        days, _ = restart(1, payload=dict(saved, samples=damaged_rows))
        failed |= _check("t4b a store that lost rows within a day to cleaning reads the full window", days == [28], days)

        # Each direction's key follows its own scaling
        key_charge, key_discharge = my_predbat.rate_grid_scan_key(False), my_predbat.rate_grid_scan_key(True)
        scaling_discharge = my_predbat.battery_rate_max_scaling_discharge
        my_predbat.battery_rate_max_scaling_discharge = scaling_discharge + 0.1
        failed |= _check("t4b discharge scaling changes the discharge key only", my_predbat.rate_grid_scan_key(True) != key_discharge and my_predbat.rate_grid_scan_key(False) == key_charge, (key_discharge, my_predbat.rate_grid_scan_key(True)))
        my_predbat.battery_rate_max_scaling_discharge = scaling_discharge

        # A change while running: the next save keeps the key the stored samples were measured under, so a restart before
        # the next scan still reads the full window; the next scan reads the full window itself and stamps the new key
        my_predbat.battery_loss = my_predbat.battery_loss - 0.05
        my_predbat.battery_rate_grid_last_save["charge"] = my_predbat.now_utc - timedelta(minutes=BATTERY_RATE_GRID_RESAVE_MINUTES)
        my_predbat.now_utc += timedelta(minutes=30)
        _update(my_predbat, inv)
        saved_live = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t4b a save after a live change keeps the measured key", saved_live.get("scan_key") == saved.get("scan_key"), saved_live.get("scan_key"))
        days_before = list(inv.days[False])
        my_predbat.now_utc += timedelta(minutes=BATTERY_RATE_GRID_SCAN_MINUTES + 1)
        _update(my_predbat, inv)
        failed |= _check("t4b the next scan after a live change reads the full window", inv.days[False][len(days_before) :] == [28], inv.days[False])
        saved_live = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t4b and stamps the new key", saved_live.get("scan_key") == my_predbat.rate_grid_scan_key(False), saved_live.get("scan_key"))

        print("Test 4c: a direction whose scans never find evidence still restarts with a short scan (#6287)")
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t4c")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()  # the discharge scans find nothing, as on a system that never force-exports
        for _ in range(26 * 12):
            my_predbat.battery_rate_grid_update(inv, True)
            my_predbat.now_utc += timedelta(minutes=5)
        failed |= _check("t4c two scans in 26 hours", inv.days.get(True) == [28, 2], inv.days.get(True))
        failed |= _check("t4c no evidence store written", run_async(storage.load("predbat", "battery_rate_grid_discharge")) is None)
        record = run_async(storage.load("predbat", "battery_rate_grid_discharge_empty_scan"))
        failed |= _check("t4c no-evidence record saved", isinstance(record, dict) and isinstance(record.get("last_success"), str) and record.get("scan_key") == my_predbat.rate_grid_scan_key(True), record)

        def restart_empty(setup=None, record_payload=None, evidence=None, fails=0):
            """
            A new instance an hour after the original's clock, on a store holding only the no-evidence record (or
            `record_payload`, and the evidence store `evidence` when given); returns the days its first discharge scan read.
            """
            restarts[0] += 1
            store = make_test_storage(my_predbat, tmpdir + "/t4c_{}".format(restarts[0]))
            run_async(store.save("predbat", "battery_rate_grid_discharge_empty_scan", record_payload or record, format="json"))
            if evidence is not None:
                run_async(store.save("predbat", "battery_rate_grid_discharge", evidence, format="json"))
            restarted = _make()
            restarted.now_utc = my_predbat.now_utc + timedelta(hours=1)
            if setup:
                setup(restarted)
            restarted.components = MockStorageComponents(FlakyStorage(store, fails))
            scanner = FakeInverter()
            restarted.battery_rate_grid_update(scanner, True)
            return scanner.days.get(True)

        days = restart_empty()
        failed |= _check("t4c a restart reads 2 days", days == [2], days)
        failed |= _check("t4c a battery_loss change reads the full window", restart_empty(lossier_battery) == [28])
        future = (my_predbat.now_utc + timedelta(days=2)).strftime(TIME_FORMAT)
        failed |= _check("t4c a record ahead of the clock reads the full window", restart_empty(record_payload=dict(record, last_success=future)) == [28])
        # A store written before this change holds evidence but no last success: the record does not stand in for it
        failed |= _check("t4c evidence without a last success reads the full window", restart_empty(evidence={"samples": _samples(my_predbat)}) == [28])
        # The two evidence store reads and then the record read all fail
        failed |= _check("t4c a failed record read reads the full window", restart_empty(fails=3) == [28])
        # An evidence store that read back but whose samples were all dropped by cleaning reads the full window: the record
        # does not stand in for it
        damaged = {"samples": {sorted(_samples(my_predbat))[0]: "not a sample list"}, "last_success": record["last_success"], "scan_key": record["scan_key"]}
        failed |= _check("t4c a fully damaged evidence store reads the full window", restart_empty(evidence=damaged) == [28])

        # A store that has read back, with no evidence in it, keeps the last success itself: no record is written alongside
        restarts[0] += 1
        store = make_test_storage(my_predbat, tmpdir + "/t4c_{}".format(restarts[0]))
        run_async(store.save("predbat", "battery_rate_grid_discharge", {"samples": {}, "last_success": record["last_success"], "scan_key": record["scan_key"]}, format="json"))
        loaded = _make()
        loaded.now_utc = my_predbat.now_utc + timedelta(days=2)
        loaded.components = MockStorageComponents(store)
        loaded.battery_rate_grid_update(FakeInverter(), True)
        failed |= _check("t4c a loaded empty store writes no record", run_async(store.load("predbat", "battery_rate_grid_discharge_empty_scan")) is None)
        failed |= _check("t4c and saves its own last success", (run_async(store.load("predbat", "battery_rate_grid_discharge")) or {}).get("last_success") != record["last_success"])

        # Once evidence is found and saved the record is retired, so a restart whose evidence reads fail does not use it
        inv.scan[True] = _samples(my_predbat)
        my_predbat.now_utc += timedelta(minutes=BATTERY_RATE_GRID_SCAN_MINUTES)
        _settle(my_predbat, inv, cycles=4)
        failed |= _check("t4c evidence saved", bool((run_async(storage.load("predbat", "battery_rate_grid_discharge")) or {}).get("samples")))
        retired = run_async(storage.load("predbat", "battery_rate_grid_discharge_empty_scan"))
        failed |= _check("t4c record retired", retired == {"retired": True}, retired)
        failed |= _check("t4c a retired record reads the full window", restart_empty(record_payload=retired, fails=2) == [28])

        print("Test 5: auto off - the manual curve is used, the evidence is still gathered and stored")
        my_predbat = _make({"battery_charge_power_curve": {100: 0.5}, "battery_rate_grid_auto": False})
        storage = make_test_storage(my_predbat, tmpdir + "/t5")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        _settle(my_predbat, inv)
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

        print("Test 6b: a storage outage longer than one cycle does not overwrite the stored year")
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t6b")
        old_day = charge_day(my_predbat.now_utc, 200 * 24 * 60, my_predbat.local_tz)
        run_async(storage.save("predbat", "battery_rate_grid_charge", {"samples": {old_day: [[50.0, 12.0, 0.4]]}, "updated": None}, format="json"))
        my_predbat.components = MockStorageComponents(FlakyStorage(storage, 2))
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        my_predbat.battery_rate_grid_update(inv, False)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t6b nothing saved while the store cannot be read", saved == {"samples": {old_day: [[50.0, 12.0, 0.4]]}, "updated": None}, saved and sorted(saved.get("samples", {}).keys()))
        my_predbat.now_utc += timedelta(minutes=5)
        my_predbat.battery_rate_grid_update(inv, False)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t6b merged once readable", isinstance(saved, dict) and old_day in saved.get("samples", {}) and len(saved.get("samples", {})) == 5, saved and sorted(saved.get("samples", {}).keys()))

        print("Test 6b2: a long outage with scans that find nothing does not save an empty store over the stored year (#6287)")
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t6b2")
        stored_year = {"samples": {old_day: [[50.0, 12.0, 0.4]]}, "updated": None}
        run_async(storage.save("predbat", "battery_rate_grid_charge", stored_year, format="json"))
        my_predbat.components = MockStorageComponents(FlakyStorage(storage, 1000))
        inv = FakeInverter()
        inv.scan[False] = {}
        for _ in range(12):
            my_predbat.battery_rate_grid_update(inv, False)
            my_predbat.now_utc += timedelta(minutes=61)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t6b2 stored year kept", saved == stored_year, saved)

        print("Test 6c: a new install's empty store is saved once the reads have come back empty on separate cycles")
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t6c")
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        inv.scan[False] = _samples(my_predbat)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t6c not saved on the first cycle", run_async(storage.load("predbat", "battery_rate_grid_charge")) is None)
        for _ in range(3):
            my_predbat.now_utc += timedelta(minutes=6)
            my_predbat.battery_rate_grid_update(inv, False)
        saved = run_async(storage.load("predbat", "battery_rate_grid_charge"))
        failed |= _check("t6c saved later", isinstance(saved, dict) and len(saved.get("samples", {})) == 4, saved)

        print("Test 8: with no manual curves a gap in the evidence is interpolated, not filled to the highest allowed rate")
        my_predbat = _make()
        my_predbat.components = MockStorageComponents(make_test_storage(my_predbat, tmpdir + "/t8"))
        inv = FakeInverter()
        gap = {}
        for d in range(4):
            day = charge_day(my_predbat.now_utc, d * 24 * 60 + 60, my_predbat.local_tz)
            gap[day] = [[soc + 0.4, temp + 0.3, rate] for temp in range(5, 25) for soc, rate in ((20, 0.9), (60, 0.5))]
        inv.scan[False] = gap
        _update(my_predbat, inv)
        value = rate_grid_curve(my_predbat.battery_charge_rate_grid, 15)[40]
        failed |= _check("t8 interpolated", my_predbat.battery_rate_grid_source.get("charge") == "learned" and 0.6 < value < 0.8, (my_predbat.battery_rate_grid_source, value))

        print("Test 9: the per-curve lookup caches are bounded")
        failed |= _check("t9 outer", rate_curve_function.cache_info().maxsize <= 64, rate_curve_function.cache_info())
        failed |= _check("t9 inner", rate_curve_function((1.0,) * 101 + (1.0, float("inf"))).cache_info().maxsize <= 2048)

        print("Test 10: debug dumps carry the learned grids but not the evidence or derived grids")
        dump = yaml.unsafe_load(my_predbat.create_debug_yaml(write_file=False))
        for key in ("battery_rate_grid_samples", "battery_rate_grid_cache", "battery_rate_grid_manual", "battery_charge_rate_grid", "battery_discharge_rate_grid"):
            failed |= _check("t10 excluded " + key, key not in dump)
        failed |= _check("t10 learned kept", "battery_rate_grid_learned" in dump)

        print("Test 11: the scan windows come from apps.yaml, a failed scan is retried hourly, and a gap widens the next window")
        my_predbat = _make({"battery_rate_grid_history_days": 14, "battery_rate_grid_scan_days": 3})
        my_predbat.components = MockStorageComponents(make_test_storage(my_predbat, tmpdir + "/t11"))
        inv = FakeInverter()
        inv.scan[False] = None  # the history fetch fails
        my_predbat.battery_rate_grid_update(inv, False)
        my_predbat.now_utc += timedelta(minutes=30)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t11 no retry within the hour", inv.days[False] == [14], inv.days)
        my_predbat.now_utc += timedelta(minutes=31)
        inv.scan[False] = _samples(my_predbat)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t11 retried with the full window, as nothing has been read yet", inv.days[False] == [14, 14], inv.days)
        my_predbat.now_utc += timedelta(minutes=61)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t11 no hourly scan once one has succeeded", inv.days[False] == [14, 14], inv.days)
        my_predbat.now_utc += timedelta(days=1)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t11 daily window from apps.yaml", inv.days[False] == [14, 14, 3], inv.days)
        my_predbat.now_utc += timedelta(days=4)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t11 a four-day gap widens the window", inv.days[False] == [14, 14, 3, 5], inv.days)
        my_predbat.now_utc += timedelta(days=40)
        my_predbat.battery_rate_grid_update(inv, False)
        failed |= _check("t11 the window never exceeds the history setting", inv.days[False][-1] == 14, inv.days)

        print("Test 12: an upgrade rescans the history - stored days it covers are replaced, older ones are kept")
        # Discharge evidence stored by a version that measured exports at an export cap just under the discharge rating as a
        # dip (0.96). After the restart the first scan reads the full window and finds the same days at 1.0.
        my_predbat = _make()
        storage = make_test_storage(my_predbat, tmpdir + "/t12")
        covered = [charge_day(my_predbat.now_utc, d * 24 * 60 + 60, my_predbat.local_tz) for d in range(6)]
        partial_day = charge_day(my_predbat.now_utc, 6 * 24 * 60 + 60, my_predbat.local_tz)
        old_day = charge_day(my_predbat.now_utc, 12 * 24 * 60 + 60, my_predbat.local_tz)
        stale = {day: [[60.0, 15.0, 0.96]] for day in covered + [old_day]}
        run_async(storage.save("predbat", "battery_rate_grid_discharge", {"samples": stale, "updated": None}, format="json"))
        my_predbat.components = MockStorageComponents(storage)
        inv = FakeInverter()
        inv.scan[True] = {day: [[60.0, 15.0, 1.0]] for day in covered}
        inv.curve_scan_coverage = {"grid_discharge": partial_day}  # the history reached part way into this day
        my_predbat.battery_rate_grid_update(inv, True)
        failed |= _check("t12 the first scan after a restart reads the full window", inv.days.get(True) == [28], inv.days)
        saved = run_async(storage.load("predbat", "battery_rate_grid_discharge"))
        saved_samples = (saved or {}).get("samples", {})
        failed |= _check("t12 covered days replaced", all(saved_samples.get(day) == [[60.0, 15.0, 1.0]] for day in covered), saved_samples)
        failed |= _check("t12 a day past the history is kept", saved_samples.get(old_day) == [[60.0, 15.0, 0.96]], saved_samples)

        print("Test 7: the options")
        failed |= _check("t7 new option", "battery_rate_grid_auto" in APPS_SCHEMA and "battery_rate_grid_history_days" in APPS_SCHEMA and "battery_rate_grid_scan_days" in APPS_SCHEMA)
        failed |= _check("t7 removed option", "battery_temperature_charge_curve_auto" not in APPS_SCHEMA)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failed
