# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for battery_temperature_charge_curve_auto - learning the cold-battery charge curve from history."""

import shutil
import tempfile
from datetime import date, timedelta

from tests.test_infra import TestHAInterface, MockStorageComponents, make_test_storage, run_async
from predbat import PredBat
from inverter import Inverter
from utils import clean_battery_temperature_samples, merge_battery_temperature_samples, learn_battery_temperature_curve, overlay_battery_temperature_curve, find_battery_temperature_cap

TEMP_SENSOR = "sensor.battery_temperature"
HISTORY_DAYS = 3

# Hour of day -> (status, charge_rate W, battery_power W, soc %, temperature C) for each test day.
# Only hours 1 and 2 are clean full-rate charges below 80% SoC; every other charging hour is one the learner must ignore.
HOUR_PLAN = {
    1: ("Charging", 2600, -1300, 30, 5.4),  # cold: learned as 1.3 kW / 10 kWh = 0.13 C at 5 degrees
    2: ("Charging", 2600, -2600, 50, 12.7),  # warmer: learned as 0.26 C at 12 degrees
    3: ("Charging", 2600, -1000, 85, 7.0),  # above 80% SoC, the SoC taper not the temperature
    4: ("Charging", 1000, -1000, 50, 8.0),  # rate written below the maximum (low power mode)
    5: ("Charging", 2600, -2600, 50, 0.0),  # an exact 0 is a missing reading
    6: ("Charging", 2600, -2600, 50, 21.0),  # 20 and above is never learned
    7: ("Exporting", 2600, 2600, 50, 7.0),  # not charging
    8: ("Charging", 2600, -10, 50, 9.0),  # charge finished, almost no power
}
IDLE = ("Idle", 0, 0, 50, 15.0)


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def _install_history(my_predbat, enabled=True):
    """
    Mock the HA history of the five sensors the learner reads, HISTORY_DAYS days of 5-minute samples following HOUR_PLAN.
    """
    history = {"sensor.soc_percent": [], "number.charge_rate": [], "sensor.battery_power": [], TEMP_SENSOR: [], "predbat.status": []}
    base_time = my_predbat.midnight_utc - timedelta(days=HISTORY_DAYS)
    for minutes in range(0, HISTORY_DAYS * 24 * 60, 5):
        timestamp = base_time + timedelta(minutes=minutes)
        stamp = timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")
        status, rate, power, soc, temp = HOUR_PLAN.get(timestamp.hour, IDLE)
        history["sensor.soc_percent"].append({"state": str(soc), "last_updated": stamp, "attributes": {"unit_of_measurement": "%"}})
        history["number.charge_rate"].append({"state": rate, "last_updated": stamp, "attributes": {"unit_of_measurement": "W"}})
        history["sensor.battery_power"].append({"state": power, "last_updated": stamp, "attributes": {"unit_of_measurement": "W"}})
        history[TEMP_SENSOR].append({"state": temp, "last_updated": stamp, "attributes": {"unit_of_measurement": "°C"}})
        history["predbat.status"].append({"state": status, "last_updated": stamp, "attributes": {}})

    def mock_get_history(entity_id, now=None, days=30):
        """Return the mocked history, or nothing when history is switched off."""
        if enabled and entity_id in history:
            return [history[entity_id]]
        return None

    my_predbat.ha_interface.get_history = mock_get_history


def _make_predbat():
    """A fresh PredBat set up like test_find_charge_curve, plus the temperature sensor."""
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = True
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 48 * 60
    my_predbat.max_days_previous = 7
    my_predbat.args["soc_percent"] = ["sensor.soc_percent"]
    my_predbat.args["charge_rate"] = ["number.charge_rate"]
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [False]
    my_predbat.args["battery_temperature_history"] = TEMP_SENSOR
    my_predbat.battery_loss = 1.0
    return my_predbat


def _make_inverter(my_predbat):
    """Inverter 0 with a 10 kWh battery and a 2.6 kW maximum charge rate."""
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    return inv


def _test_pure_helpers():
    """clean/merge/learn/overlay on hand-built samples."""
    failed = False
    today = date(2026, 11, 14)

    print("Test 1: samples read back from storage are cleaned")
    cleaned = clean_battery_temperature_samples({"2026-11-13": {"5": [1.3, 10], "x": [1, 1], "6": "junk", "7": [1.0, 0], "25": [1.0, 5]}, "not-a-day": {"5": [1, 1]}, "2026-11-12": "junk"})
    failed |= _check("t1 cleaned", cleaned == {"2026-11-13": {5: [1.3, 10]}}, cleaned)
    failed |= _check("t1 junk", clean_battery_temperature_samples(["junk"]) == {} and clean_battery_temperature_samples(None) == {})

    print("Test 2: a scanned day replaces a stored one only when it holds at least as many minutes; old days are dropped")
    stored = {"2026-11-10": {5: [13.0, 100]}, "2026-11-12": {5: [1.3, 10]}, "2025-01-01": {5: [1.0, 10]}}
    scanned = {"2026-11-10": {5: [1.3, 10]}, "2026-11-12": {5: [2.6, 20]}, "2026-11-13": {12: [2.6, 10]}}
    merged = merge_battery_temperature_samples(stored, scanned, today)
    failed |= _check("t2 partial day kept", merged["2026-11-10"] == {5: [13.0, 100]}, merged)
    failed |= _check("t2 fuller day replaced", merged["2026-11-12"] == {5: [2.6, 20]}, merged)
    failed |= _check("t2 new day added", merged["2026-11-13"] == {12: [2.6, 10]}, merged)
    failed |= _check("t2 old day dropped", "2025-01-01" not in merged, merged)
    failed |= _check("t2 inputs untouched", stored["2026-11-12"] == {5: [1.3, 10]} and "2026-11-13" not in stored)

    print("Test 3: a degree needs enough minutes, and recent days outweigh old ones")
    samples = {"2026-11-14": {5: [0.2 * 20, 20], 9: [0.1 * 10, 10]}, "2026-10-15": {5: [0.1 * 20, 20]}}
    curve, minutes = learn_battery_temperature_curve(samples, today)
    failed |= _check("t3 too few minutes", 9 not in curve, curve)
    failed |= _check("t3 minutes", minutes == {5: 40}, minutes)
    # 30 days old weighs half: (4 + 2 * 0.5) / (20 + 20 * 0.5) = 0.1667
    failed |= _check("t3 weighted", curve == {5: 0.17}, curve)

    print("Test 4: overlay - the learned range replaces the configured curve, gaps interpolated, colder filled, 0 left alone")
    configured = {15: 0.2, 8: 0.5, 3: 0.04, 0: 0.0}
    curve = overlay_battery_temperature_curve(configured, {5: 0.13, 12: 0.26})
    failed |= _check("t4 learned", curve[5] == 0.13 and curve[12] == 0.26, curve)
    failed |= _check("t4 interpolated", curve[6] == round(0.13 + 0.13 / 7, 2) and curve[11] == round(0.13 + 0.13 * 6 / 7, 2), curve)
    failed |= _check("t4 learned range replaces configured", curve[8] == round(0.13 + 0.13 * 3 / 7, 2), curve)
    failed |= _check("t4 configured kept outside the learned range", curve[3] == 0.04 and curve[15] == 0.2, curve)
    failed |= _check("t4 colder filled", curve[1] == 0.13 and curve[2] == 0.13 and curve[4] == 0.13, curve)
    failed |= _check("t4 zero and warm untouched", curve[0] == 0.0 and 20 not in curve and 13 not in curve, curve)
    failed |= _check("t4 configured not modified", configured == {15: 0.2, 8: 0.5, 3: 0.04, 0: 0.0}, configured)
    failed |= _check("t4 nothing learned", overlay_battery_temperature_curve(configured, {}) == configured)
    curve = overlay_battery_temperature_curve({}, {-3: 0.05, 4: 0.12})
    failed |= _check("t4 zero not interpolated", 0 not in curve and curve[-1] == 0.07 and curve[1] == 0.09, curve)
    # The cap the prediction uses follows the learned value: 0.13 C of 10 kWh is 1.3 kW
    failed |= _check("t4 cap", abs(find_battery_temperature_cap(5.4, overlay_battery_temperature_curve({}, {5: 0.13}), 10.0, 1.0) * 60 - 1.3) < 1e-6)
    return failed


def _test_scan(my_predbat):
    """find_battery_temperature_samples keeps only clean full-rate charging minutes below 80% SoC."""
    failed = False
    print("Test 5: the history scan learns only the clean charging minutes")
    _install_history(my_predbat)
    inv = _make_inverter(my_predbat)
    samples = inv.find_battery_temperature_samples()
    failed |= _check("t5 samples", isinstance(samples, dict) and samples, samples)
    if failed:
        return failed
    temps = {temp for day_bins in samples.values() for temp in day_bins}
    failed |= _check("t5 only clean degrees", temps == {5, 12}, temps)
    curve, minutes = learn_battery_temperature_curve(samples, my_predbat.now_utc.date())
    failed |= _check("t5 rates", curve == {5: 0.13, 12: 0.26}, curve)
    failed |= _check("t5 minutes", minutes.get(5, 0) >= (HISTORY_DAYS - 1) * 60, minutes)

    print("Test 6: battery_power_invert is honoured")
    my_predbat.args["battery_power_invert"] = [True]
    failed |= _check("t6 inverted", inv.find_battery_temperature_samples() == {}, "")
    my_predbat.args["battery_power_invert"] = [False]

    print("Test 7: missing settings or history learn nothing")
    my_predbat.args.pop("battery_temperature_history")
    failed |= _check("t7 no sensor", inv.find_battery_temperature_samples() is None)
    my_predbat.args["battery_temperature_history"] = TEMP_SENSOR
    _install_history(my_predbat, enabled=False)
    failed |= _check("t7 no history", inv.find_battery_temperature_samples() is None)
    return failed


def _test_update(my_predbat):
    """battery_temperature_curve_auto_update: overlay, publish, save, hourly scans and restore after a restart."""
    failed = False
    tmpdir = tempfile.mkdtemp()
    saved_components = my_predbat.components
    try:
        storage = make_test_storage(my_predbat, tmpdir)
        my_predbat.components = MockStorageComponents(storage)
        _install_history(my_predbat)
        inv = _make_inverter(my_predbat)
        scans = []
        real_scan = inv.find_battery_temperature_samples

        def counting_scan():
            """Count the history scans."""
            scans.append(1)
            return real_scan()

        inv.find_battery_temperature_samples = counting_scan

        print("Test 8: the learned curve is laid over the configured one and published")
        my_predbat.battery_temperature_charge_curve = {15: 0.2, 3: 0.04}
        my_predbat.battery_temperature_curve_auto_update(inv)
        curve = my_predbat.battery_temperature_charge_curve
        failed |= _check("t8 curve", curve.get(5) == 0.13 and curve.get(12) == 0.26 and curve.get(15) == 0.2 and curve.get(3) == 0.04 and curve.get(1) == 0.13, curve)
        state = my_predbat.get_state_wrapper(my_predbat.prefix + ".battery_temperature_charge_curve")
        attributes = my_predbat.get_state_wrapper(my_predbat.prefix + ".battery_temperature_charge_curve", attribute="learned")
        failed |= _check("t8 published", str(state) == "2" and attributes == {"5": 0.13, "12": 0.26}, "{} {}".format(state, attributes))
        saved = run_async(storage.load("predbat", "battery_temperature_curve"))
        failed |= _check("t8 saved", isinstance(saved, dict) and clean_battery_temperature_samples(saved.get("samples")) == my_predbat.battery_temperature_samples, saved)

        print("Test 9: the next cycle re-applies the overlay without scanning the history again")
        my_predbat.battery_temperature_charge_curve = {15: 0.2}  # as fetch_config_options() resets it
        my_predbat.battery_temperature_curve_auto_update(inv)
        failed |= _check("t9 one scan", len(scans) == 1, scans)
        failed |= _check("t9 overlay", my_predbat.battery_temperature_charge_curve.get(5) == 0.13, my_predbat.battery_temperature_charge_curve)
        my_predbat.battery_temperature_curve_last_scan = my_predbat.now_utc - timedelta(minutes=61)
        my_predbat.battery_temperature_curve_auto_update(inv)
        failed |= _check("t9 hourly rescan", len(scans) == 2, scans)

        print("Test 10: after a restart the curve comes back from storage, even with no history")
        my_predbat.battery_temperature_samples = {}
        my_predbat.battery_temperature_samples_loaded = False
        my_predbat.battery_temperature_curve_learned = {}
        my_predbat.battery_temperature_curve_last_scan = None
        my_predbat.battery_temperature_charge_curve = {}
        _install_history(my_predbat, enabled=False)
        my_predbat.battery_temperature_curve_auto_update(inv)
        curve = my_predbat.battery_temperature_charge_curve
        failed |= _check("t10 restored", curve.get(5) == 0.13 and curve.get(12) == 0.26, curve)

        print("Test 11: with no storage the curve is still learned from history")
        my_predbat.components = MockStorageComponents(None)
        my_predbat.battery_temperature_samples = {}
        my_predbat.battery_temperature_samples_loaded = False
        my_predbat.battery_temperature_curve_learned = {}
        my_predbat.battery_temperature_curve_last_scan = None
        my_predbat.battery_temperature_charge_curve = {}
        _install_history(my_predbat)
        my_predbat.battery_temperature_curve_auto_update(inv)
        failed |= _check("t11 learned", my_predbat.battery_temperature_charge_curve.get(12) == 0.26, my_predbat.battery_temperature_charge_curve)
    finally:
        my_predbat.components = saved_components
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failed


def _test_config(my_predbat):
    """The apps.yaml flag is read by fetch_config_options and is off by default."""
    failed = False
    print("Test 12: battery_temperature_charge_curve_auto is off unless set")
    my_predbat.args.pop("battery_temperature_charge_curve_auto", None)
    my_predbat.fetch_config_options()
    failed |= _check("t12 default off", my_predbat.battery_temperature_charge_curve_auto is False)
    my_predbat.args["battery_temperature_charge_curve_auto"] = True
    my_predbat.fetch_config_options()
    failed |= _check("t12 on", my_predbat.battery_temperature_charge_curve_auto is True)
    my_predbat.args.pop("battery_temperature_charge_curve_auto", None)
    return failed


def run_battery_temperature_curve_auto_tests(my_predbat):
    """
    Run the battery_temperature_charge_curve_auto tests on their own PredBat, so the mocked history cannot leak.
    """
    print("**** Running battery_temperature_curve_auto tests ****")
    failed = _test_pure_helpers()
    own_predbat = _make_predbat()
    failed |= _test_scan(own_predbat)
    failed |= _test_update(own_predbat)
    failed |= _test_config(own_predbat)
    return failed
