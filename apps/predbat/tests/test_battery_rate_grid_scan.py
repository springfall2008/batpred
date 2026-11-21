# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for BatteryCurve.find_rate_grid_samples - full-rate runs cut into 5-minute rate grid segments."""

from datetime import timedelta

from tests.test_infra import TestHAInterface
from predbat import PredBat
from inverter import Inverter
from battery_curve import charge_day

TEMP_SENSOR = "sensor.battery_temperature"
RATED_W = 2600
BLOCK_START_HOURS = 26  # the block starts this many hours before now


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def _install(my_predbat, block, temperature_default=15.0, block_start_hours=BLOCK_START_HOURS):
    """
    One-minute history for two days: idle, except `block`, a list of per-minute
    (status, rate W, battery power W, soc %, temperature, grid W) starting BLOCK_START_HOURS before now.
    """
    sensors = ["sensor.soc_percent", "number.charge_rate", "number.discharge_rate", "sensor.battery_power", TEMP_SENSOR, "predbat.status", "sensor.grid_power"]
    history = {name: [] for name in sensors}
    start = my_predbat.now_utc - timedelta(hours=48)
    block_start = my_predbat.now_utc - timedelta(hours=block_start_hours)
    minutes = 48 * 60
    for minute in range(minutes):
        stamp_time = start + timedelta(minutes=minute)
        index = int((stamp_time - block_start).total_seconds() // 60)
        if 0 <= index < len(block):
            status, rate, power, soc, temp, grid = block[index]
        else:
            status, rate, power, soc, temp, grid = ("Idle", 0, 0, 50, temperature_default, 0)
        stamp = stamp_time.strftime("%Y-%m-%dT%H:%M:%S%z")
        history["sensor.soc_percent"].append({"state": str(soc), "last_updated": stamp, "attributes": {"unit_of_measurement": "%"}})
        history["number.charge_rate"].append({"state": rate if status == "Charging" else 0, "last_updated": stamp, "attributes": {"unit_of_measurement": "W"}})
        history["number.discharge_rate"].append({"state": rate if status != "Charging" else 0, "last_updated": stamp, "attributes": {"unit_of_measurement": "W"}})
        history["sensor.battery_power"].append({"state": power, "last_updated": stamp, "attributes": {"unit_of_measurement": "W"}})
        history[TEMP_SENSOR].append({"state": temp, "last_updated": stamp, "attributes": {"unit_of_measurement": "°C"}})
        history["predbat.status"].append({"state": status, "last_updated": stamp, "attributes": {}})
        history["sensor.grid_power"].append({"state": grid, "last_updated": stamp, "attributes": {"unit_of_measurement": "W"}})

    def mock_get_history(entity_id, now=None, days=30):
        """Return the mocked history."""
        if entity_id in history:
            return [history[entity_id]]
        return None

    my_predbat.ha_interface.get_history = mock_get_history


def _make(args=None):
    """
    A PredBat and inverter 0 (10 kWh, 2.6 kW rated) reading the mocked sensors.

    With args (apps.yaml settings such as inverter_limit_export) the inverter takes its rates from them, as Inverter
    derives them, instead of having them set to RATED_W.
    """
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = True
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.max_days_previous = 2
    my_predbat.args["soc_percent"] = ["sensor.soc_percent"]
    my_predbat.args["charge_rate"] = ["number.charge_rate"]
    my_predbat.args["discharge_rate"] = ["number.discharge_rate"]
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [False]
    my_predbat.args["grid_power"] = ["sensor.grid_power"]
    my_predbat.args["battery_temperature_history"] = TEMP_SENSOR
    my_predbat.battery_loss = 1.0
    my_predbat.battery_rate_max_scaling = 1.0
    my_predbat.battery_rate_max_scaling_discharge = 1.0
    if args:
        my_predbat.args.update(args)
    inv = Inverter(my_predbat, 0)
    if not args:
        inv.battery_rate_max_charge = RATED_W / 60000
        inv.battery_rate_max_discharge = RATED_W / 60000
        inv.battery_rate_max_export = RATED_W / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    return my_predbat, inv


def _make_two(battery_power):
    """
    A PredBat with two 10 kWh, 2.6 kW inverters. Each has its own SoC and charge/discharge rate sensors (inverter 1's
    mirror inverter 0's history), and battery_power is set as given, e.g. a site-wide sensor on inverter 0 and 0 on
    inverter 1, the way GE Cloud configures an EMS.
    """
    my_predbat, inv0 = _make()
    my_predbat.args["soc_percent"] = ["sensor.soc_percent", "sensor.soc_percent_1"]
    my_predbat.args["charge_rate"] = ["number.charge_rate", "number.charge_rate_1"]
    my_predbat.args["discharge_rate"] = ["number.discharge_rate", "number.discharge_rate_1"]
    my_predbat.args["battery_power"] = battery_power
    my_predbat.args["battery_power_invert"] = [False, False]
    inv1 = Inverter(my_predbat, 1)
    inv1.battery_rate_max_charge = RATED_W / 60000
    inv1.battery_rate_max_discharge = RATED_W / 60000
    inv1.battery_rate_max_export = RATED_W / 60000
    inv1.soc_max = 10.0
    inv1.battery_scaling = 1.0
    my_predbat.num_inverters = 2
    my_predbat.inverters = [inv0, inv1]
    return my_predbat, inv0


def _install_two(my_predbat, block):
    """Install a block for inverter 0's sensors and give inverter 1's sensors the same history."""
    _install(my_predbat, block)
    single = my_predbat.ha_interface.get_history
    aliases = {"sensor.soc_percent_1": "sensor.soc_percent", "number.charge_rate_1": "number.charge_rate", "number.discharge_rate_1": "number.discharge_rate"}

    def mock_get_history(entity_id, now=None, days=30):
        """Inverter 1's sensors read inverter 0's history."""
        return single(aliases.get(entity_id, entity_id), now=now, days=days)

    my_predbat.ha_interface.get_history = mock_get_history


def _charge(minutes, power=-1560, temp=15.3, soc=40):
    """A full-rate charge block."""
    return [("Charging", RATED_W, power, soc, temp, -RATED_W) for _ in range(minutes)]


def _export(minutes, power=1560, grid=1000, status="Exporting", rate=RATED_W):
    """A full-rate forced export block."""
    return [(status, rate, power, 60, 15.3, grid) for _ in range(minutes)]


def _pin_local_time(my_predbat, hour, minute=0):
    """Pin Predbat's clock (now_utc, midnight_utc, minutes_now) to today's hour:minute local time, so a block a fixed number of hours back lands at a known time of day."""
    wall_clock = my_predbat.now_utc.replace(tzinfo=None, hour=hour, minute=minute, second=0, microsecond=0)
    my_predbat.update_time(print=False, now=my_predbat.local_tz.localize(wall_clock))


def _segments(samples):
    """All segments in a samples dict."""
    return [entry for entries in (samples or {}).values() for entry in entries]


def _scan(block, discharge=False, setup=None, temperature_default=15.0):
    """Install a block and run the scan."""
    my_predbat, inv = _make()
    if setup:
        setup(my_predbat)
    _install(my_predbat, block, temperature_default=temperature_default)
    return my_predbat, inv.find_rate_grid_samples(discharge)


def run_battery_rate_grid_scan_tests(my_predbat_unused=None):
    """Run the rate grid evidence scan tests."""
    failed = False

    print("Test 1: a 25-minute full-rate charge gives three 5-minute segments")
    # Now is pinned to 18:00 local, so the block (26 hours back) runs 16:00-16:25 yesterday, after yesterday's noon: one
    # noon-to-noon day, named by yesterday's date. With the real clock, a now of 13:45 or 13:50 put the block across the
    # boundary instead - Test 1a pins that case
    my_predbat, samples = _scan(_charge(25), setup=lambda p: _pin_local_time(p, 18))
    segments = _segments(samples)
    yesterday = (my_predbat.now_utc.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    failed |= _check("t1 count", len(segments) == 3, segments)
    failed |= _check("t1 values", all(abs(s[0] - 40) < 1e-6 and abs(s[1] - 15.3) < 1e-6 and abs(s[2] - 0.6) < 1e-6 for s in segments), segments)
    failed |= _check("t1 day", list(samples.keys()) == [yesterday], (list(samples.keys()), yesterday))

    print("Test 1a: a run across local noon is split between the two noon-to-noon days, each segment by its centre minute")
    # Now 13:50, so the block runs 11:50-12:15 yesterday and its segment centres are 11:57, 12:02 and 12:07. 11:57 is before
    # yesterday's noon, so it belongs to the day that started at noon the day before
    my_predbat, samples = _scan(_charge(25), setup=lambda p: _pin_local_time(p, 13, 50))
    yesterday = (my_predbat.now_utc.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (my_predbat.now_utc.date() - timedelta(days=2)).strftime("%Y-%m-%d")
    counts = {day: len(entries) for day, entries in (samples or {}).items()}
    failed |= _check("t1a split", counts == {day_before: 1, yesterday: 2}, (counts, day_before, yesterday))

    print("Test 1b: a scan reads only the days it is asked for")
    my_predbat, inv = _make()
    _install(my_predbat, _charge(25))
    failed |= _check("t1b one day misses a run 26 hours ago", _segments(inv.find_rate_grid_samples(False, days=1)) == [])
    failed |= _check("t1b two days finds it", len(_segments(inv.find_rate_grid_samples(False, days=2))) == 3)

    print("Test 1c: a window longer than days_previous is read past the history cache (tracked=False), a short one through it")
    my_predbat, inv = _make()
    _install(my_predbat, _charge(25))
    calls = []
    real = my_predbat.get_history_wrapper

    def recording(entity_id, days=30, required=True, tracked=True):
        """Record each history read's window and tracked flag."""
        calls.append((days, tracked))
        return real(entity_id, days=days, required=required, tracked=tracked)

    my_predbat.get_history_wrapper = recording
    inv.find_rate_grid_samples(False, days=28)
    failed |= _check("t1c long read untracked", calls and all(days == 28 and tracked is False for days, tracked in calls), calls)
    calls.clear()
    inv.find_rate_grid_samples(False, days=2)
    failed |= _check("t1c short read tracked", calls and all(days == 2 and tracked is True for days, tracked in calls), calls)

    print("Test 1d: asking for more days than the history holds - the padded days are neither evidence nor coverage")
    my_predbat, inv = _make()
    # Only 48 hours of history, and it starts in the middle of a full-rate charge: padding the 26 days before it with
    # that first reading must not become one long fake run
    _install(my_predbat, _charge(25), block_start_hours=48)
    segments = _segments(inv.find_rate_grid_samples(False, days=28))
    failed |= _check("t1d no fake run from padding", 0 < len(segments) <= 3, len(segments))
    covered = inv.curve_scan_coverage.get("grid_charge")
    failed |= _check("t1d coverage is the real history", covered is not None and covered >= charge_day(my_predbat.now_utc, 49 * 60, getattr(my_predbat, "local_tz", None)), covered)

    print("Test 1e: minute_data and minute_data_state with pad=False stop at the oldest record")
    from utils import minute_data, minute_data_state

    now = my_predbat.now_utc
    records = [{"state": "5", "last_updated": (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S%z")} for minutes in (2000, 1000, 10)]
    padded, _ = minute_data(records, 5, now, "state", "last_updated", backwards=True)
    unpadded, _ = minute_data(records, 5, now, "state", "last_updated", backwards=True, pad=False)
    failed |= _check("t1e minute_data padded to the window", max(padded) == 5 * 24 * 60 - 1, max(padded))
    failed |= _check("t1e minute_data unpadded stops at the oldest record", max(unpadded) == 2000 and len(unpadded) == 2001, (max(unpadded), len(unpadded)))
    states = [{"state": "Charging", "last_updated": (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S%z")} for minutes in (2000, 10)]
    failed |= _check("t1e minute_data_state padded to the window", max(minute_data_state(states, 5, now, "state", "last_updated")) == 5 * 24 * 60 - 1)
    unpadded_state = minute_data_state(states, 5, now, "state", "last_updated", pad=False)
    failed |= _check("t1e minute_data_state unpadded stops at the oldest record", max(unpadded_state) == 2000 and len(unpadded_state) == 2001, (max(unpadded_state), len(unpadded_state)))

    print("Test 2: a 14-minute run is too short")
    _, samples = _scan(_charge(14))
    failed |= _check("t2", _segments(samples) == [], samples)

    print("Test 3: a minute where the battery did not charge splits the run")
    block = _charge(15) + _charge(1, power=0) + _charge(14)
    _, samples = _scan(block)
    failed |= _check("t3", len(_segments(samples)) == 1, samples)

    print("Test 4: an exact 0 reading splits the run on an install with a temperature sensor")
    block = _charge(15) + _charge(1, temp=0) + _charge(14)
    _, samples = _scan(block)
    failed |= _check("t4", len(_segments(samples)) == 1, samples)

    print("Test 5: an install whose temperature always reads exactly 0 has no temperature")
    _, samples = _scan(_charge(25, temp=0), temperature_default=0)
    segments = _segments(samples)
    failed |= _check("t5", len(segments) == 3 and all(s[1] is None for s in segments), segments)

    print("Test 6: forced exports - only ones that reached the grid count")
    _, samples = _scan(_export(25), discharge=True)
    failed |= _check("t6 counted", len(_segments(samples)) == 3, samples)
    failed |= _check("t6 rate relative to the discharge rating", all(abs(s[2] - 0.6) < 1e-6 for s in _segments(samples)), samples)
    _, samples = _scan(_export(25, grid=0), discharge=True)
    failed |= _check("t6 export did not reach the grid", _segments(samples) == [], samples)
    _, samples = _scan(_export(25, status="Discharging"), discharge=True)
    failed |= _check("t6 demand discharge is not a forced export", _segments(samples) == [], samples)
    _, samples = _scan(_export(25), discharge=False)
    failed |= _check("t6 exports are not charge evidence", _segments(samples) == [], samples)

    print("Test 7: battery_rate_max_scaling is divided out")

    def scaled(my_predbat):
        """Scale the charge rate to 0.8."""
        my_predbat.battery_rate_max_scaling = 0.8

    _, samples = _scan(_charge(25, power=-2080), setup=scaled)
    segments = _segments(samples)
    failed |= _check("t7", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 7b: an export held back by the export limit is evidence of no battery limit (the limit, not SoC or temperature, held it)")

    my_predbat, inv = _make()
    inv.export_limit = 2000 / 60000
    _install(my_predbat, _export(25, grid=2000))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t7b limited", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)
    my_predbat, inv = _make()
    inv.export_limit = 2000 / 60000
    _install(my_predbat, _export(25, grid=1000))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t7b below the limit is a battery limit", len(segments) == 3 and all(abs(s[2] - 0.6) < 1e-6 for s in segments), segments)
    my_predbat, inv = _make()
    inv.export_limit = 2000 / 60000
    my_predbat.args["grid_power_invert"] = [True]
    _install(my_predbat, _export(25, grid=-2000))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t7b inverted grid sensor", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 7c: without grid_power history an ignored export cannot be told apart, so no discharge evidence is gathered")

    def no_grid(my_predbat):
        """No grid_power sensor."""
        my_predbat.args.pop("grid_power", None)

    _, samples = _scan(_export(25), discharge=True, setup=no_grid)
    failed |= _check("t7c", samples is None, samples)

    print("Test 7d: a charge command the inverter ignored (battery charging from PV, nothing imported) is not evidence")
    _, samples = _scan([("Charging", RATED_W, -1560, 40, 15.3, 0) for _ in range(25)])
    failed |= _check("t7d", _segments(samples) == [], samples)
    _, samples = _scan(_charge(25), setup=no_grid)
    failed |= _check("t7d charge without grid history still counts", len(_segments(samples)) == 3, samples)

    print("Test 8: a run written below the full rate does not count")
    block = [("Charging", 1000, -1000, 40, 15.3, -1000) for _ in range(25)]
    _, samples = _scan(block)
    failed |= _check("t8", _segments(samples) == [], samples)

    print("Test 9: a site-wide battery_power on inverter 0 (0 on the others) is measured against the whole fleet's rate")
    # Both inverters charge at their full 2.6 kW and the site-wide sensor reads the 5.2 kW total: that is the rated rate,
    # not twice it, so it must not be divided by inverter 0's 2.6 kW alone (#6226)
    my_predbat, inv = _make_two(["sensor.battery_power", 0])
    _install_two(my_predbat, _charge(25, power=-2 * RATED_W))
    segments = _segments(inv.find_rate_grid_samples(False))
    failed |= _check("t9 charge", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)
    my_predbat, inv = _make_two(["sensor.battery_power", 0])
    _install_two(my_predbat, _export(25, power=2 * RATED_W, grid=4000))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t9 discharge", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 9b: battery_power missing on inverter 1 (not 0) still learns from inverter 0 alone")
    my_predbat, inv = _make_two(["sensor.battery_power"])
    _install_two(my_predbat, _charge(25, power=-RATED_W))
    segments = _segments(inv.find_rate_grid_samples(False))
    failed |= _check("t9b", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 9c: with a site-wide battery_power, another inverter's missing history stops the learning rather than learning from inverter 0 alone")
    # Inverter 0 alone would divide the site's power by its own maximum, the ~N x samples #6226 fixes
    my_predbat, inv = _make_two(["sensor.battery_power", 0])
    _install_two(my_predbat, _charge(25, power=-2 * RATED_W))
    two = my_predbat.ha_interface.get_history

    def no_soc_1(entity_id, now=None, days=30):
        """Inverter 1's SoC sensor has no recorded history."""
        return None if entity_id == "sensor.soc_percent_1" else two(entity_id, now=now, days=days)

    my_predbat.ha_interface.get_history = no_soc_1
    samples = inv.find_rate_grid_samples(False)
    failed |= _check("t9c missing history", samples is None, samples)
    my_predbat, inv = _make_two(["sensor.battery_power", 0])
    my_predbat.args["soc_percent"] = ["sensor.soc_percent"]
    _install_two(my_predbat, _charge(25, power=-2 * RATED_W))
    samples = inv.find_rate_grid_samples(False)
    failed |= _check("t9c missing setting", samples is None, samples)

    print("Test 9e: the fleet is every inverter, as the plan's fetch loop sums them, even one whose soc_max reads 0")
    # Dropping it would leave inverter 0 alone against the site-wide power: the ~N x samples again
    my_predbat, inv = _make_two(["sensor.battery_power", 0])
    my_predbat.inverters[1].soc_max = 0
    _install_two(my_predbat, _charge(25, power=-2 * RATED_W))
    segments = _segments(inv.find_rate_grid_samples(False))
    failed |= _check("t9e", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 9d: an empty battery_power entry is a site-wide sensor on inverter 0, as a 0 is")
    my_predbat, inv = _make_two(["sensor.battery_power", ""])
    _install_two(my_predbat, _charge(25, power=-2 * RATED_W))
    segments = _segments(inv.find_rate_grid_samples(False))
    failed |= _check("t9d", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 10: battery_temperature_history as a list is read as the fleet's average temperature, as the plan reads it")
    second_sensor = "sensor.battery_temperature_2"
    # A reading of exactly 0 is a missing one: with another sensor reading, that one is used
    for sensors, offset, expected in (([TEMP_SENSOR], 10, 15.3), ([TEMP_SENSOR, second_sensor], 10, 20.3), ([TEMP_SENSOR, second_sensor], None, 15.3)):
        my_predbat, inv = _make()
        my_predbat.args["battery_temperature_history"] = sensors
        _install(my_predbat, _charge(25))
        single = my_predbat.ha_interface.get_history

        def mock_get_history(entity_id, now=None, days=30, single=single, offset=offset):
            """The second sensor reads `offset` degrees warmer than the first, or exactly 0 when offset is None."""
            if entity_id != second_sensor:
                return single(entity_id, now=now, days=days)
            return [[dict(record, state=0 if offset is None else float(record["state"]) + offset) for record in single(TEMP_SENSOR, now=now, days=days)[0]]]

        my_predbat.ha_interface.get_history = mock_get_history
        segments = _segments(inv.find_rate_grid_samples(False))
        failed |= _check("t10 {} sensors, second {}".format(len(sensors), "at 0" if offset is None else "+{}".format(offset)), len(segments) == 3 and all(abs(s[1] - expected) < 1e-6 for s in segments), segments)

    print("Test 11: battery_temperature_history given as a list does not break the scan (#6222)")

    def temperature_list(my_predbat):
        """apps.yaml gives battery_temperature_history as a list, one sensor per inverter."""
        my_predbat.args["battery_temperature_history"] = [TEMP_SENSOR]

    _, samples = _scan(_charge(25), setup=temperature_list)
    segments = _segments(samples)
    failed |= _check("t11 count", len(segments) == 3, segments)
    failed |= _check("t11 temperature", all(abs(s[1] - 15.3) < 1e-6 for s in segments), segments)

    print("Test 12: inverter_limit_charge caps the charge rate the evidence is measured against")
    limits = {"battery_rate_max": [RATED_W], "inverter_limit_charge": [1300]}
    my_predbat, inv = _make(limits)
    failed |= _check("t12 charge rate capped", abs(inv.battery_rate_max_charge * 60000 - 1300) < 1e-6, inv.battery_rate_max_charge * 60000)
    _install(my_predbat, [("Charging", 1300, -1300, 40, 15.3, -1300) for _ in range(25)])
    segments = _segments(inv.find_rate_grid_samples(False))
    failed |= _check("t12 a charge at the capped rate is full rate", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 13: inverter_limit_export below the discharge rating - forced exports run at the export cap")
    limits = {"battery_rate_max": [RATED_W], "inverter_limit_export": [1300]}
    my_predbat, inv = _make(limits)
    failed |= _check("t13 export rate capped", abs(inv.battery_rate_max_export * 60000 - 1300) < 1e-6 and abs(inv.battery_rate_max_discharge * 60000 - RATED_W) < 1e-6)
    # Predbat writes the cap as the discharge rate: the battery at the cap, 300 W of it covering the house, 1000 W to the grid
    _install(my_predbat, _export(25, power=1300, grid=1000, rate=1300))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t13 an export at the cap is evidence", len(segments) == 3, segments)
    failed |= _check("t13 an export at the cap records no battery limit", all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)
    # The battery tapering below the cap is a real limit, recorded in kW against the discharge rating the plan scales the grid by
    my_predbat, inv = _make(limits)
    _install(my_predbat, _export(25, power=650, grid=350, rate=1300))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t13 a taper below the cap is recorded in kW of the discharge rating", len(segments) == 3 and all(abs(s[2] - 650 / RATED_W) < 1e-6 for s in segments), segments)
    # PV adding to the export (the grid sees more than the cap) does not hide the taper
    my_predbat, inv = _make(limits)
    _install(my_predbat, _export(25, power=650, grid=1350, rate=1300))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t13 a taper with PV pushing the export past the cap", len(segments) == 3 and all(abs(s[2] - 650 / RATED_W) < 1e-6 for s in segments), segments)
    # inverter_limit_export 2000 W, export_limit 1000 W: the battery gives 1200 W while the grid export sits at the 1000 W limit
    # (200 W to the house). Well under its own cap, but the export limit held it back, not the battery: no battery limit
    my_predbat, inv = _make({"battery_rate_max": [RATED_W], "inverter_limit_export": [2000]})
    inv.export_limit = 1000 / 60000
    _install(my_predbat, _export(25, power=1200, grid=1000, rate=2000))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t13 held at export_limit below the export cap is not a battery limit", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)
    # An export written below the cap (low power export) is not full rate
    my_predbat, inv = _make(limits)
    _install(my_predbat, _export(25, power=650, rate=650))
    failed |= _check("t13 an export below the cap is not full rate", _segments(inv.find_rate_grid_samples(True)) == [])

    print("Test 14: inverter_limit_export above the discharge rating - the rate is still recorded against the discharge rating")
    # The house-load step reads the grid against the discharge rating, so a taper the battery held at 1000 W is 1000 / 1300 there
    limits = {"battery_rate_max": [RATED_W], "inverter_limit_discharge": [1300], "inverter_limit_export": [RATED_W]}
    my_predbat, inv = _make(limits)
    _install(my_predbat, _export(25, power=1000, rate=RATED_W))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t14 taper", len(segments) == 3 and all(abs(s[2] - 1000 / 1300) < 1e-4 for s in segments), segments)
    my_predbat, inv = _make(limits)
    _install(my_predbat, _export(25, power=1300, rate=1300))
    segments = _segments(inv.find_rate_grid_samples(True))
    failed |= _check("t14 an export written at the discharge rating (under the cap) asks the battery for all it can give", len(segments) == 3 and all(abs(s[2] - 1.0) < 1e-6 for s in segments), segments)

    print("Test 15: an export cap of 0 means no forced exports - no evidence, no history read, and not None (which retries hourly)")
    my_predbat, inv = _make({"battery_rate_max": [RATED_W], "inverter_limit_export": [0]})
    failed |= _check("t15 cap is 0", inv.battery_rate_max_export == 0, inv.battery_rate_max_export)
    _install(my_predbat, _charge(25) + _export(25, power=1560, rate=0))
    inv.curve_scan_coverage = {"grid_discharge": "2000-01-01"}  # left from an earlier scan when the cap was not 0
    reads = []
    installed = my_predbat.ha_interface.get_history

    def counting(entity_id, now=None, days=30):
        """Count the history reads."""
        reads.append(entity_id)
        return installed(entity_id, now=now, days=days)

    my_predbat.ha_interface.get_history = counting
    samples = inv.find_rate_grid_samples(True)
    failed |= _check("t15 no evidence", samples == {}, samples)
    failed |= _check("t15 no history read", reads == [], reads)
    failed |= _check("t15 coverage cleared, so the merge keeps the stored days", inv.curve_scan_coverage.get("grid_discharge") is None, inv.curve_scan_coverage)
    failed |= _check("t15 charge evidence is still gathered", len(_segments(inv.find_rate_grid_samples(False))) == 3, reads)
    return failed
