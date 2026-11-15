# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for battery_power_curve_update - the SoC charge/discharge curve and its per-day samples, kept in storage in auto mode."""

import shutil
import tempfile
from datetime import timedelta

from tests.test_infra import MockStorageComponents, make_test_storage, run_async
from const import BATTERY_POWER_CURVE_RESCAN_MINUTES, BATTERY_POWER_CURVE_RETRY_MINUTES, TIME_FORMAT

from battery_curve import charge_day

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
    "battery_power_curve_samples",
    "battery_power_curve_source",
    "battery_power_curve_updated",
    "now_utc",
    "components",
]
DEFAULT = {100: 0.9}


def _day(my_predbat, days_ago):
    """The evidence day (charge_day) a number of days back."""
    return charge_day(my_predbat.now_utc, days_ago * 24 * 60, my_predbat.local_tz)


def _samples(day, curve):
    """Samples for one day that learn exactly curve with the fake learner: one step per point."""
    return {day: {point: [value, 1, 5.0] for point, value in curve.items()}}


class FakeInverter:
    """
    An inverter whose scan returns the samples a test sets, and whose learner is the plain mean of every point in the
    samples it is given - the real evidence rules are covered by the find_charge_curve tests, these cover the storage.
    """

    def __init__(self):
        """Nothing scanned and no calls yet."""
        self.scan = {False: {}, True: {}}
        self.result = {False: {}, True: {}}
        self.calls = {False: 0, True: 0}
        self.learned_from = {False: None, True: None}

    def find_charge_curve(self, discharge):
        """Non-auto mode: the curve set for this direction."""
        self.calls[discharge] += 1
        return dict(self.result[discharge])

    def find_charge_curve_samples(self, discharge):
        """Auto mode scan: the samples set for this direction (None means missing settings or history)."""
        self.calls[discharge] += 1
        value = self.scan[discharge]
        return None if value is None else {day: {point: list(entry) for point, entry in points.items()} for day, points in value.items()}

    def learn_charge_curve(self, samples, discharge):
        """The mean of each point across all the days given."""
        self.learned_from[discharge] = samples
        sums, counts = {}, {}
        for points in samples.values():
            for point, (value_sum, steps, _minutes) in points.items():
                sums[point] = sums.get(point, 0) + value_sum
                counts[point] = counts.get(point, 0) + steps
        return {point: round(sums[point] / counts[point], 2) for point in sums}


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


def _restart(my_predbat, auto=True, default=None, discharge_default=None):
    """Lose the in-memory curve state, as a restart does."""
    my_predbat.battery_charge_power_curve = {}
    my_predbat.battery_discharge_power_curve = {}
    my_predbat.battery_charge_power_curve_auto = auto
    my_predbat.battery_discharge_power_curve_auto = auto
    my_predbat.battery_charge_power_curve_default = dict(default or {})
    my_predbat.battery_discharge_power_curve_default = dict(discharge_default or {})
    my_predbat.computed_charge_curve = False
    my_predbat.computed_discharge_curve = False
    my_predbat.battery_power_curve_loaded = {}
    my_predbat.battery_power_curve_last_scan = {}
    my_predbat.battery_power_curve_samples = {}
    my_predbat.battery_power_curve_source = {}
    my_predbat.battery_power_curve_updated = {}


def _sensor(my_predbat, kind, attribute=None):
    """The published binary sensor for a power curve, or one of its attributes."""
    my_predbat.publish_battery_curve_sensors()
    return my_predbat.get_state_wrapper("binary_sensor.{}_battery_{}_power_curve".format(my_predbat.prefix, kind), attribute=attribute)


def _stored(storage, name):
    """(curve, samples) saved under name, with int keys, or (None, None)."""
    data = run_async(storage.load("predbat", name))
    if not isinstance(data, dict):
        return None, None
    curve = {int(key): value for key, value in data.get("curve", {}).items()}
    samples = {day: {int(point): entry for point, entry in points.items()} for day, points in data.get("samples", {}).items()}
    return curve, samples


def _rescan_due(my_predbat, kind, minutes):
    """Make the last scan this many minutes ago."""
    my_predbat.battery_power_curve_last_scan[kind] = my_predbat.now_utc - timedelta(minutes=minutes)


def _run(my_predbat, storage):
    """The update cases, with a real local-file storage."""
    failed = False
    inv = FakeInverter()
    day0, day1, day7, day70 = _day(my_predbat, 0), _day(my_predbat, 1), _day(my_predbat, 7), _day(my_predbat, 70)

    print("Test 1: not auto - computed once, as before, and never saved")
    _restart(my_predbat, auto=False)
    inv.result[False] = {100: 0.5, 99: 0.6}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t1 curve", my_predbat.battery_charge_power_curve == {100: 0.5, 99: 0.6}, my_predbat.battery_charge_power_curve)
    failed |= _check("t1 one scan", inv.calls[False] == 1, inv.calls)
    failed |= _check("t1 not saved", _stored(storage, "battery_charge_power_curve")[0] is None)

    print("Test 2: not auto - nothing found uses the default and stops retrying")
    _restart(my_predbat, auto=False, default=DEFAULT)
    inv.calls[False] = 0
    inv.result[False] = {}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t2 default", my_predbat.battery_charge_power_curve == DEFAULT and my_predbat.computed_charge_curve, my_predbat.battery_charge_power_curve)
    failed |= _check("t2 one scan", inv.calls[False] == 1, inv.calls)

    print("Test 3: auto - nothing found retries hourly, using the default meanwhile, which is never saved as the curve")
    _restart(my_predbat, default=DEFAULT)
    inv.calls[False] = 0
    my_predbat.battery_power_curve_update(inv, discharge=False)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t3 default", my_predbat.battery_charge_power_curve == DEFAULT, my_predbat.battery_charge_power_curve)
    failed |= _check("t3 default not saved", _stored(storage, "battery_charge_power_curve")[0] in (None, {}), _stored(storage, "battery_charge_power_curve"))
    failed |= _check("t3 one scan an hour", inv.calls[False] == 1, inv.calls)
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RETRY_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t3 retried after an hour, not a day", inv.calls[False] == 2, inv.calls)

    print("Test 4: auto - a scan's samples are saved with the curve learned from them, then re-scanned once a day")
    _restart(my_predbat)
    inv.calls[False] = 0
    inv.scan[False] = _samples(day1, {99: 0.5, 98: 0.6})
    my_predbat.battery_power_curve_update(inv, discharge=False)
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RETRY_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    curve, samples = _stored(storage, "battery_charge_power_curve")
    failed |= _check("t4 curve", my_predbat.battery_charge_power_curve == {99: 0.5, 98: 0.6}, my_predbat.battery_charge_power_curve)
    failed |= _check("t4 saved", curve == {99: 0.5, 98: 0.6} and samples == {day1: {99: [0.5, 1, 5.0], 98: [0.6, 1, 5.0]}}, (curve, samples))
    failed |= _check("t4 daily rescan", inv.calls[False] == 1, inv.calls)

    print("Test 5: auto - after a restart the stored days are kept and a new day's scan is added to them, not replacing them")
    _restart(my_predbat)
    inv.scan[False] = _samples(day0, {99: 0.3})
    my_predbat.battery_power_curve_update(inv, discharge=False)
    curve, samples = _stored(storage, "battery_charge_power_curve")
    failed |= _check("t5 both days stored", set(samples) == {day0, day1}, samples)
    failed |= _check("t5 learned from both days", my_predbat.battery_charge_power_curve == {99: 0.4, 98: 0.6}, my_predbat.battery_charge_power_curve)

    print("Test 6: auto - a re-scan of a day already stored replaces it rather than counting it twice")
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RESCAN_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t6 not doubled", my_predbat.battery_charge_power_curve == {99: 0.4, 98: 0.6} and _stored(storage, "battery_charge_power_curve")[1][day0] == {99: [0.3, 1, 5.0]}, my_predbat.battery_charge_power_curve)

    print("Test 7: auto - a scan that finds nothing keeps the stored days, and missing history (None) leaves them untouched")
    inv.scan[False] = {}
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RESCAN_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t7 kept", my_predbat.battery_charge_power_curve == {99: 0.4, 98: 0.6}, my_predbat.battery_charge_power_curve)
    inv.scan[False] = None
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RESCAN_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t7 none leaves samples", set(_stored(storage, "battery_charge_power_curve")[1]) == {day0, day1}, _stored(storage, "battery_charge_power_curve"))

    print("Test 8: auto - days over the maximum age are dropped; the charge curve is kept when nothing is left")
    run_async(storage.save("predbat", "battery_charge_power_curve", {"curve": {"99": 0.4}, "samples": {day70: {"99": [0.4, 1, 5.0]}}}, format="json"))
    _restart(my_predbat)
    inv.scan[False] = {}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    curve, samples = _stored(storage, "battery_charge_power_curve")
    failed |= _check("t8 old day dropped", samples == {}, samples)
    failed |= _check("t8 charge curve kept", my_predbat.battery_charge_power_curve == {99: 0.4} and curve == {99: 0.4}, (my_predbat.battery_charge_power_curve, curve))

    print("Test 9: auto - the discharge curve stops being used once its evidence has aged out")
    run_async(storage.save("predbat", "battery_discharge_power_curve", {"curve": {"5": 0.5}, "samples": {day70: {"5": [0.5, 1, 5.0]}}}, format="json"))
    _restart(my_predbat, discharge_default={5: 0.9})
    inv.scan[True] = {}
    my_predbat.battery_power_curve_update(inv, discharge=True)
    curve, samples = _stored(storage, "battery_discharge_power_curve")
    failed |= _check("t9 expired to the default", my_predbat.battery_discharge_power_curve == {5: 0.9}, my_predbat.battery_discharge_power_curve)
    failed |= _check("t9 expiry saved", curve == {} and samples == {}, (curve, samples))
    run_async(storage.save("predbat", "battery_discharge_power_curve", {"curve": {"5": 0.5}, "samples": {day7: {"5": [0.5, 1, 5.0]}}}, format="json"))
    _restart(my_predbat)
    my_predbat.battery_power_curve_update(inv, discharge=True)
    failed |= _check("t9 recent evidence kept", my_predbat.battery_discharge_power_curve == {5: 0.5}, my_predbat.battery_discharge_power_curve)
    failed |= _check("t9 charge untouched", _stored(storage, "battery_charge_power_curve")[0] == {99: 0.4})

    print("Test 10: junk in storage is ignored")
    run_async(storage.save("predbat", "battery_charge_power_curve", {"curve": "junk", "samples": {"not-a-day": {"99": [1, 1, 1]}, day1: {"x": "junk"}}}, format="json"))
    _restart(my_predbat)
    inv.scan[False] = {}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t10 junk", my_predbat.battery_charge_power_curve == {} and my_predbat.battery_power_curve_samples.get("charge") == {}, my_predbat.battery_charge_power_curve)

    print("Test 11: not auto, no curve in apps.yaml - the computed or default curve survives fetch_config_options() every cycle")
    saved_args = {key: my_predbat.args.get(key) for key in ("battery_charge_power_curve", "battery_charge_power_curve_default", "battery_discharge_power_curve")}
    try:
        for key in saved_args:
            my_predbat.args.pop(key, None)
        my_predbat.fetch_config_options()
        failed |= _check("t11 charge curve defaults to auto", my_predbat.battery_charge_power_curve_auto is True)
        failed |= _check("t11 discharge curve defaults to off", my_predbat.battery_discharge_power_curve_auto is False)

        my_predbat.args["battery_charge_power_curve"] = {}
        _restart(my_predbat, auto=False)
        inv.result[False] = {100: 0.5}
        my_predbat.battery_power_curve_update(inv, discharge=False)
        my_predbat.fetch_config_options()
        my_predbat.battery_power_curve_update(inv, discharge=False)
        failed |= _check("t11 computed kept", my_predbat.battery_charge_power_curve == {100: 0.5}, my_predbat.battery_charge_power_curve)

        my_predbat.args["battery_charge_power_curve_default"] = {str(soc): value for soc, value in DEFAULT.items()}
        _restart(my_predbat, auto=False, default=DEFAULT)
        inv.result[False] = {}
        my_predbat.battery_power_curve_update(inv, discharge=False)
        my_predbat.fetch_config_options()
        failed |= _check("t11 default kept", my_predbat.battery_charge_power_curve == DEFAULT, my_predbat.battery_charge_power_curve)

        my_predbat.args["battery_charge_power_curve"] = {"100": 0.3}
        my_predbat.fetch_config_options()
        failed |= _check("t11 apps.yaml curve wins", my_predbat.battery_charge_power_curve == {100: 0.3} and not my_predbat.battery_charge_power_curve_auto, my_predbat.battery_charge_power_curve)

        my_predbat.args["battery_charge_power_curve"] = {}
        my_predbat.args.pop("battery_charge_power_curve_default")
        _restart(my_predbat, auto=False)
        my_predbat.battery_charge_power_curve = {100: 0.3}
        my_predbat.fetch_config_options()
        failed |= _check("t11 not yet computed resets", my_predbat.battery_charge_power_curve == {}, my_predbat.battery_charge_power_curve)
    finally:
        for key, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
        my_predbat.fetch_config_options()

    print("Test 12b: with the scan's coverage, a re-scan that now finds less on a fully covered day replaces the stored day")
    run_async(storage.save("predbat", "battery_charge_power_curve", {"curve": {"99": 0.5}, "samples": {day1: {"99": [0.5, 1, 5.0], "98": [0.6, 1, 5.0]}, day7: {"99": [0.5, 1, 5.0]}}}, format="json"))
    _restart(my_predbat)
    inv.scan[False] = _samples(day1, {99: 0.3})
    inv.curve_scan_coverage = {"charge": _day(my_predbat, 3)}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    curve, samples = _stored(storage, "battery_charge_power_curve")
    failed |= _check("t12b covered day replaced", samples.get(day1) == {99: [0.3, 1, 5.0]}, samples)
    failed |= _check("t12b day before the scan kept", samples.get(day7) == {99: [0.5, 1, 5.0]}, samples)
    del inv.curve_scan_coverage

    print("Test 12c: a failed read at start-up does not lose the stored days - the store is read again before the first save")
    run_async(storage.save("predbat", "battery_charge_power_curve", {"curve": {"99": 0.5}, "samples": {day7: {"99": [0.5, 1, 5.0]}}}, format="json"))
    _restart(my_predbat)
    my_predbat.components = MockStorageComponents(FlakyStorage(storage, 1))
    inv.scan[False] = _samples(day0, {99: 0.3})
    my_predbat.battery_power_curve_update(inv, discharge=False)
    curve, samples = _stored(storage, "battery_charge_power_curve")
    failed |= _check("t12c stored day kept", set(samples) == {day0, day7}, samples)
    failed |= _check("t12c learned from both", my_predbat.battery_charge_power_curve == {99: 0.4}, my_predbat.battery_charge_power_curve)
    my_predbat.components = MockStorageComponents(storage)

    print("Test 13: the curve sensors say whether a learned or manual curve is in use, where it came from and when it was learned")
    run_async(storage.save("predbat", "battery_charge_power_curve", {"curve": {}, "samples": {}}, format="json"))
    run_async(storage.save("predbat", "battery_discharge_power_curve", {"curve": {}, "samples": {}}, format="json"))
    _restart(my_predbat, default=DEFAULT)
    inv.scan[False] = {}
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t13 default is off", _sensor(my_predbat, "charge") == "off" and _sensor(my_predbat, "charge", "source") == "default" and _sensor(my_predbat, "charge", "updated") is None, _sensor(my_predbat, "charge", "source"))
    learned_at = my_predbat.now_utc.strftime(TIME_FORMAT)
    inv.scan[False] = _samples(day0, {99: 0.5, 98: 0.7})
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RETRY_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t13 learned is on", _sensor(my_predbat, "charge") == "on" and _sensor(my_predbat, "charge", "curve") == {"98": 0.7, "99": 0.5}, _sensor(my_predbat, "charge", "curve"))
    failed |= _check("t13 learned date", _sensor(my_predbat, "charge", "updated") == learned_at and _sensor(my_predbat, "charge", "days") == 1 and _sensor(my_predbat, "charge", "auto") is True, _sensor(my_predbat, "charge", "updated"))
    failed |= _check("t13 date saved", run_async(storage.load("predbat", "battery_charge_power_curve")).get("updated") == learned_at)
    # A restart a day later restores the curve with the date it was learned, and the same curve found again keeps that date
    _restart(my_predbat, default=DEFAULT)
    my_predbat.now_utc = my_predbat.now_utc + timedelta(days=1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t13 date restored", _sensor(my_predbat, "charge") == "on" and _sensor(my_predbat, "charge", "updated") == learned_at, _sensor(my_predbat, "charge", "updated"))
    # A changed curve (a second day's evidence) moves the date on
    inv.scan[False] = _samples(day1, {99: 0.3})
    _rescan_due(my_predbat, "charge", BATTERY_POWER_CURVE_RESCAN_MINUTES + 1)
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t13 new curve, new date", _sensor(my_predbat, "charge", "updated") == my_predbat.now_utc.strftime(TIME_FORMAT), _sensor(my_predbat, "charge", "updated"))
    failed |= _check("t13 discharge not learned is off", _sensor(my_predbat, "discharge") == "off" and _sensor(my_predbat, "discharge", "source") == "none", _sensor(my_predbat, "discharge", "source"))
    saved_args = {key: my_predbat.args.get(key) for key in ("battery_charge_power_curve",)}
    try:
        my_predbat.args["battery_charge_power_curve"] = {"100": 0.3}
        my_predbat.fetch_config_options()
        failed |= _check(
            "t13 apps.yaml curve is on as manual",
            _sensor(my_predbat, "charge") == "on"
            and _sensor(my_predbat, "charge", "source") == "manual"
            and _sensor(my_predbat, "charge", "curve") == {"100": 0.3}
            and _sensor(my_predbat, "charge", "updated") is None
            and _sensor(my_predbat, "charge", "auto") is False,
            _sensor(my_predbat, "charge", "source"),
        )
    finally:
        for key, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
        my_predbat.fetch_config_options()

    print("Test 12: with no storage auto mode still learns")
    my_predbat.components = MockStorageComponents(None)
    _restart(my_predbat)
    inv.scan[False] = _samples(day0, {99: 0.5})
    my_predbat.battery_power_curve_update(inv, discharge=False)
    failed |= _check("t12 learned", my_predbat.battery_charge_power_curve == {99: 0.5}, my_predbat.battery_charge_power_curve)
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
