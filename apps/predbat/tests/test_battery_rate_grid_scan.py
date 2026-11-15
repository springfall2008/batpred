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


def _make():
    """A PredBat and inverter 0 (10 kWh, 2.6 kW rated) reading the mocked sensors."""
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
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = RATED_W / 60000
    inv.battery_rate_max_discharge = RATED_W / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    return my_predbat, inv


def _charge(minutes, power=-1560, temp=15.3, soc=40):
    """A full-rate charge block."""
    return [("Charging", RATED_W, power, soc, temp, -RATED_W) for _ in range(minutes)]


def _export(minutes, power=1560, grid=1000, status="Exporting"):
    """A full-rate forced export block."""
    return [(status, RATED_W, power, 60, 15.3, grid) for _ in range(minutes)]


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
    my_predbat, samples = _scan(_charge(25))
    segments = _segments(samples)
    failed |= _check("t1 count", len(segments) == 3, segments)
    failed |= _check("t1 values", all(abs(s[0] - 40) < 1e-6 and abs(s[1] - 15.3) < 1e-6 and abs(s[2] - 0.6) < 1e-6 for s in segments), segments)
    expected_day = charge_day(my_predbat.now_utc, BLOCK_START_HOURS * 60 - 12, getattr(my_predbat, "local_tz", None))
    failed |= _check("t1 day", list(samples.keys()) == [expected_day], (list(samples.keys()), expected_day))

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

    print("Test 7b: an export held back by the export limit is not evidence (moved from the old discharge curve tests)")

    my_predbat, inv = _make()
    inv.export_limit = 2000 / 60000
    _install(my_predbat, _export(25, grid=2000))
    failed |= _check("t7b limited", _segments(inv.find_rate_grid_samples(True)) == [], "export at the limit learned")
    my_predbat, inv = _make()
    inv.export_limit = 2000 / 60000
    _install(my_predbat, _export(25, grid=1000))
    failed |= _check("t7b below the limit", len(_segments(inv.find_rate_grid_samples(True))) == 3)
    my_predbat, inv = _make()
    inv.export_limit = 2000 / 60000
    my_predbat.args["grid_power_invert"] = [True]
    _install(my_predbat, _export(25, grid=-2000))
    failed |= _check("t7b inverted grid sensor", _segments(inv.find_rate_grid_samples(True)) == [], "inverted export at the limit learned")

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
    return failed
