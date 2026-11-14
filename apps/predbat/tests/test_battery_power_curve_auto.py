# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for battery_power_curve_update - the SoC charge/discharge curve, kept in storage in auto mode."""

import shutil
import tempfile
from datetime import timedelta

from tests.test_infra import MockStorageComponents, make_test_storage, run_async
from const import BATTERY_POWER_CURVE_RESCAN_MINUTES

CURVE_A = {100: 0.5, 99: 0.6, 98: 0.8}
CURVE_B = {100: 0.4, 99: 0.5, 98: 0.7}
DEFAULT = {100: 0.9}
FIELDS = [
    "battery_charge_power_curve",
    "battery_charge_power_curve_auto",
    "battery_charge_power_curve_default",
    "battery_discharge_power_curve",
    "battery_discharge_power_curve_auto",
    "battery_discharge_power_curve_default",
    "computed_charge_curve",
    "computed_discharge_curve",
    "battery_power_curve_loaded",
    "battery_power_curve_last_scan",
    "components",
]


class FakeInverter:
    """An inverter whose find_charge_curve() returns what the test sets, and counts the calls."""

    def __init__(self):
        """Nothing found and no calls yet."""
        self.result = {False: {}, True: {}}
        self.calls = {False: 0, True: 0}

    def find_charge_curve(self, discharge):
        """Return the curve set for this direction."""
        self.calls[discharge] += 1
        return dict(self.result[discharge])


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def _restart(my_predbat, auto=True, default=None):
    """Lose the in-memory curve state, as a restart does."""
    my_predbat.battery_charge_power_curve = {}
    my_predbat.battery_discharge_power_curve = {}
    my_predbat.battery_charge_power_curve_auto = auto
    my_predbat.battery_discharge_power_curve_auto = auto
    my_predbat.battery_charge_power_curve_default = dict(default or {})
    my_predbat.battery_discharge_power_curve_default = {}
    my_predbat.computed_charge_curve = False
    my_predbat.computed_discharge_curve = False
    my_predbat.battery_power_curve_loaded = {}
    my_predbat.battery_power_curve_last_scan = {}


def _stored(storage, name):
    """The curve saved under name, with int keys, or None."""
    data = run_async(storage.load("predbat", name))
    if not isinstance(data, dict):
        return None
    return {int(key): value for key, value in data.get("curve", {}).items()}


def _run(my_predbat, storage):
    """The update cases, with a real local-file storage."""
    failed = False
    inv = FakeInverter()

    print("Test 1: not auto - computed once, as before, and never saved")
    _restart(my_predbat, auto=False)
    inv.result[False] = CURVE_A
    my_predbat.battery_power_curve_update(inv, discharge=False)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t1 curve", my_predbat.battery_charge_power_curve == CURVE_A, my_predbat.battery_charge_power_curve)
    failed |= _check("t1 one scan", inv.calls[False] == 1, inv.calls)
    failed |= _check("t1 not saved", _stored(storage, "battery_charge_power_curve") is None)

    print("Test 2: not auto - nothing found uses the default and stops retrying")
    _restart(my_predbat, auto=False, default=DEFAULT)
    inv.calls[False] = 0
    inv.result[False] = {}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t2 default", my_predbat.battery_charge_power_curve == DEFAULT and my_predbat.computed_charge_curve, my_predbat.battery_charge_power_curve)
    failed |= _check("t2 one scan", inv.calls[False] == 1, inv.calls)

    print("Test 3: auto - nothing found yet retries every cycle, using the default meanwhile, which is not saved")
    _restart(my_predbat, default=DEFAULT)
    inv.calls[False] = 0
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t3 default", my_predbat.battery_charge_power_curve == DEFAULT, my_predbat.battery_charge_power_curve)
    failed |= _check("t3 default not saved", _stored(storage, "battery_charge_power_curve") is None)
    _restart(my_predbat)
    inv.calls[False] = 0
    for _cycle in range(3):
        my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t3 retries", inv.calls[False] == 3 and my_predbat.battery_charge_power_curve == {}, inv.calls)

    print("Test 4: auto - a curve found is used and saved, then only re-scanned once a day")
    inv.result[False] = CURVE_A
    inv.calls[False] = 0
    my_predbat.battery_power_curve_update(inv, discharge=False)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t4 curve", my_predbat.battery_charge_power_curve == CURVE_A, my_predbat.battery_charge_power_curve)
    failed |= _check("t4 saved", _stored(storage, "battery_charge_power_curve") == CURVE_A, _stored(storage, "battery_charge_power_curve"))
    failed |= _check("t4 one scan", inv.calls[False] == 1, inv.calls)

    print("Test 5: auto - a later scan that finds nothing keeps the curve and the saved copy")
    inv.result[False] = {}
    my_predbat.battery_power_curve_last_scan["charge"] = my_predbat.now_utc - timedelta(minutes=BATTERY_POWER_CURVE_RESCAN_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t5 rescanned", inv.calls[False] == 2, inv.calls)
    failed |= _check("t5 kept", my_predbat.battery_charge_power_curve == CURVE_A, my_predbat.battery_charge_power_curve)
    failed |= _check("t5 saved kept", _stored(storage, "battery_charge_power_curve") == CURVE_A)

    print("Test 6: auto - after a restart the saved curve is used at once, even with no full-rate cycle in the history")
    _restart(my_predbat, default=DEFAULT)
    inv.calls[False] = 0
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t6 restored", my_predbat.battery_charge_power_curve == CURVE_A, my_predbat.battery_charge_power_curve)
    failed |= _check("t6 still scans after restart", inv.calls[False] == 1, inv.calls)

    print("Test 7: auto - a newer curve found on a re-scan replaces the saved one")
    inv.result[False] = CURVE_B
    my_predbat.battery_power_curve_last_scan["charge"] = my_predbat.now_utc - timedelta(minutes=BATTERY_POWER_CURVE_RESCAN_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t7 replaced", my_predbat.battery_charge_power_curve == CURVE_B and _stored(storage, "battery_charge_power_curve") == CURVE_B, my_predbat.battery_charge_power_curve)

    print("Test 8: the discharge curve is kept separately")
    inv.result[True] = {5: 0.5, 4: 0.3}
    my_predbat.battery_power_curve_update(inv, discharge=True)
    failed |= _check("t8 discharge", my_predbat.battery_discharge_power_curve == {5: 0.5, 4: 0.3} and _stored(storage, "battery_discharge_power_curve") == {5: 0.5, 4: 0.3}, my_predbat.battery_discharge_power_curve)
    failed |= _check("t8 charge untouched", _stored(storage, "battery_charge_power_curve") == CURVE_B)

    print("Test 9: junk in storage is ignored")
    run_async(storage.save("predbat", "battery_charge_power_curve", {"curve": "junk"}, format="json"))
    _restart(my_predbat)
    inv.result[False] = {}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t9 junk", my_predbat.battery_charge_power_curve == {}, my_predbat.battery_charge_power_curve)

    print("Test 10: with no storage auto mode still learns")
    my_predbat.components = MockStorageComponents(None)
    _restart(my_predbat)
    inv.result[False] = CURVE_A
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t10 learned", my_predbat.battery_charge_power_curve == CURVE_A, my_predbat.battery_charge_power_curve)
    return failed


def run_battery_power_curve_auto_tests(my_predbat):
    """
    battery_power_curve_update: the auto SoC curve is kept in storage and only replaced when a scan finds data.
    """
    print("*** Running test: battery_power_curve_auto")
    saved = {field: getattr(my_predbat, field, None) for field in FIELDS}
    tmpdir = tempfile.mkdtemp()
    try:
        storage = make_test_storage(my_predbat, tmpdir)
        my_predbat.components = MockStorageComponents(storage)
        return _run(my_predbat, storage)
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)
        shutil.rmtree(tmpdir, ignore_errors=True)
