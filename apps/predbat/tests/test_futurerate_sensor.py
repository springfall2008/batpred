# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for sourcing futurerate predictions from a Home Assistant sensor.

Covers the per-side (import/export) sensor branch of FutureRate.futurerate_analysis():
default key handling (AgilePredict-shaped), custom key/attribute overrides, missing or
empty sensor data, sensor-only setups (no URL), the override relationship between
sensor and URL, variable slot durations derived from the feed itself, and the
guarantee that sensor data is NOT run through Nordpool calibration.
"""

from datetime import timedelta
from unittest.mock import patch

from futurerate import FutureRate

from tests.test_futurerate_auto import MockFutureRateBase, NORDPOOL_URL


IMPORT_SENSOR = "sensor.agilepredict_import"
EXPORT_SENSOR = "sensor.agilepredict_export"


def _make_prices(base, hours=24, rate=20.0, key_time="date_time", key_rate="agile_pred", interval_minutes=30):
    """Return a list of ``hours`` × (60/interval_minutes) price dicts starting at midnight UTC."""
    slots = int(hours * 60 / interval_minutes)
    out = []
    for i in range(slots):
        ts = base.midnight_utc + timedelta(minutes=i * interval_minutes)
        out.append({key_time: ts.isoformat(), key_rate: rate + (i * 0.1)})
    return out


def _make_sensor_future(base, import_entity=IMPORT_SENSOR, export_entity=None):
    """Construct a FutureRate wired up with one or both sensor sources."""
    if import_entity:
        base.args["futurerate_sensor_import"] = import_entity
    if export_entity:
        base.args["futurerate_sensor_export"] = export_entity
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    return FutureRate(base)


# ---------------------------------------------------------------------------
# Sensor branch: happy paths (run through real production code, no calibrate shim)
# ---------------------------------------------------------------------------


def _test_sensor_import_basic(my_predbat):
    """Import-side sensor with AgilePredict-shaped data populates mdata in p/kWh, unmodified."""
    base = MockFutureRateBase()
    prices = _make_prices(base, hours=2, rate=15.0)
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if not mdata or mdata.get(0) != 15.0:
        print("ERROR: Expected import sensor mdata[0] == 15.0 (raw, no calibration), got {}".format(mdata.get(0) if mdata else mdata))
        return True
    return False


def _test_sensor_export_basic(my_predbat):
    """Export-side sensor populates export-side mdata using the same shape options."""
    base = MockFutureRateBase()
    prices = _make_prices(base, hours=2, rate=8.5)
    base.set_entity_attr(EXPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base, import_entity=None, export_entity=EXPORT_SENSOR)

    mdata = future.futurerate_analysis_sensor("export")

    if not mdata or mdata.get(0) != 8.5:
        print("ERROR: Expected export sensor mdata[0] == 8.5 (raw, no calibration), got {}".format(mdata.get(0) if mdata else mdata))
        return True
    return False


def _test_sensor_custom_keys(my_predbat):
    """Custom attribute / time / rate keys are honoured."""
    base = MockFutureRateBase()
    base.args["futurerate_sensor_attribute"] = "forecasts"
    base.args["futurerate_sensor_time_key"] = "ts"
    base.args["futurerate_sensor_rate_key"] = "p"
    prices = _make_prices(base, hours=2, rate=22.0, key_time="ts", key_rate="p")
    base.set_entity_attr(IMPORT_SENSOR, "forecasts", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if mdata.get(0) != 22.0:
        print("ERROR: Expected import mdata[0] == 22.0 with custom keys, got {}".format(mdata.get(0)))
        return True
    return False


def _test_sensor_calibrate_not_called(my_predbat):
    """Sensor path must skip futurerate_calibrate entirely (Nordpool multiplier would inflate p/kWh data)."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_import"] = True  # would have triggered calibrate previously
    prices = _make_prices(base, hours=2, rate=15.0)
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    calibrate_calls = []
    future.futurerate_calibrate = lambda *a, **kw: calibrate_calls.append((a, kw)) or {}

    mdata = future.futurerate_analysis_sensor("import")

    if calibrate_calls:
        print("ERROR: futurerate_calibrate must NOT be called for sensor data (would inflate p/kWh by Nordpool multiplier), got {} call(s)".format(len(calibrate_calls)))
        return True
    if mdata.get(0) != 15.0:
        print("ERROR: Expected raw sensor value 15.0 to pass through, got {}".format(mdata.get(0)))
        return True
    return False


# ---------------------------------------------------------------------------
# Slot duration derivation
# ---------------------------------------------------------------------------


def _test_sensor_30min_slots_fill_gap(my_predbat):
    """30-minute feed populates the full 30-minute window even when plan_interval_minutes=5."""
    base = MockFutureRateBase()
    base.plan_interval_minutes = 5  # would have truncated each slot to 5 minutes under the old code
    prices = _make_prices(base, hours=2, rate=15.0, interval_minutes=30)
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    # First slot starts at 15.0 and runs the full 30 minutes
    if mdata.get(0) != 15.0 or mdata.get(15) != 15.0 or mdata.get(29) != 15.0:
        print("ERROR: Expected first 30-min slot fully populated at 15.0, got mdata[0]={}, mdata[15]={}, mdata[29]={}".format(mdata.get(0), mdata.get(15), mdata.get(29)))
        return True
    # Second slot at 30 minutes is 15.1
    if mdata.get(30) != 15.1:
        print("ERROR: Expected second slot (minute 30) == 15.1, got {}".format(mdata.get(30)))
        return True
    return False


def _test_sensor_15min_slots_derived(my_predbat):
    """Non-default 15-minute feed is correctly handled regardless of plan_interval_minutes."""
    base = MockFutureRateBase()
    base.plan_interval_minutes = 30
    prices = _make_prices(base, hours=1, rate=10.0, interval_minutes=15)
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    # 15-minute boundaries: 10.0 at 0, 10.1 at 15, 10.2 at 30, 10.3 at 45
    if mdata.get(0) != 10.0 or mdata.get(14) != 10.0:
        print("ERROR: Expected first 15-min slot 10.0, got mdata[0]={}, mdata[14]={}".format(mdata.get(0), mdata.get(14)))
        return True
    if mdata.get(15) != 10.1 or mdata.get(29) != 10.1:
        print("ERROR: Expected second 15-min slot 10.1, got mdata[15]={}, mdata[29]={}".format(mdata.get(15), mdata.get(29)))
        return True
    if mdata.get(45) != 10.3:
        print("ERROR: Expected fourth 15-min slot 10.3, got mdata[45]={}".format(mdata.get(45)))
        return True
    return False


# ---------------------------------------------------------------------------
# Sensor branch: degenerate inputs
# ---------------------------------------------------------------------------


def _test_sensor_missing_entity(my_predbat):
    """Sensor entity has no state at all → returns {} and flags status."""
    base = MockFutureRateBase()
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if mdata:
        print("ERROR: Expected empty dict when sensor entity missing, got {}".format(mdata))
        return True
    if not any("returned no list data" in m for m in base.log_messages):
        print("ERROR: Expected warning log about missing data, got: {}".format(base.log_messages))
        return True
    return False


def _test_sensor_empty_prices(my_predbat):
    """Sensor present but prices=[] → returns {} cleanly."""
    base = MockFutureRateBase()
    base.set_entity_attr(IMPORT_SENSOR, "prices", [])
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if mdata:
        print("ERROR: Expected empty dict for empty prices list, got {}".format(mdata))
        return True
    return False


def _test_sensor_skips_malformed_items(my_predbat):
    """Items missing keys, with bad timestamps, or non-numeric rates are skipped."""
    base = MockFutureRateBase()
    good_ts = base.midnight_utc.isoformat()
    later_ts = (base.midnight_utc + timedelta(minutes=30)).isoformat()
    prices = [
        {"date_time": good_ts, "agile_pred": 17.5},
        {"date_time": "not-a-timestamp", "agile_pred": 10.0},
        {"date_time": good_ts},
        {"agile_pred": 12.0},
        {"date_time": good_ts, "agile_pred": "not-a-number"},
        "scalar-not-a-dict",
        {"date_time": later_ts, "agile_pred": 18.5},
    ]
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if mdata.get(0) != 17.5:
        print("ERROR: Expected only the well-formed items to populate mdata, mdata[0]==17.5, got {}".format(mdata.get(0)))
        return True
    if mdata.get(30) != 18.5:
        print("ERROR: Expected mdata[30]==18.5, got {}".format(mdata.get(30)))
        return True
    return False


def _test_sensor_all_items_unparseable_warns(my_predbat):
    """Non-empty list but no item parses → returns {} and emits a diagnostic warning (don't fail silently)."""
    base = MockFutureRateBase()
    prices = [
        {"date_time": "garbage", "agile_pred": "also-garbage"},
        {"wrong_key": 1},
        "scalar",
    ]
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if mdata:
        print("ERROR: Expected empty dict when no items parseable, got {}".format(mdata))
        return True
    if not any("usable" in m.lower() for m in base.log_messages):
        print("ERROR: Expected warning log about no usable / unparseable items, got: {}".format(base.log_messages))
        return True
    return False


# ---------------------------------------------------------------------------
# Dispatch in futurerate_analysis()
# ---------------------------------------------------------------------------


def _test_dispatch_sensors_only_no_url(my_predbat):
    """No URL set, both sensors set → both sides come from sensors, URL fetch never runs."""
    base = MockFutureRateBase()
    import_prices = _make_prices(base, hours=2, rate=14.0)
    export_prices = _make_prices(base, hours=2, rate=6.0)
    base.set_entity_attr(IMPORT_SENSOR, "prices", import_prices)
    base.set_entity_attr(EXPORT_SENSOR, "prices", export_prices)
    future = _make_sensor_future(base, export_entity=EXPORT_SENSOR)

    with patch.object(future, "futurerate_analysis_new") as mock_new:
        mdata_import, mdata_export = future.futurerate_analysis({}, {})
        if mock_new.called:
            print("ERROR: futurerate_analysis_new must not be called when futurerate_url is unset")
            return True
    if mdata_import.get(0) != 14.0:
        print("ERROR: Expected sensor-only import[0] == 14.0, got {}".format(mdata_import.get(0)))
        return True
    if mdata_export.get(0) != 6.0:
        print("ERROR: Expected sensor-only export[0] == 6.0, got {}".format(mdata_export.get(0)))
        return True
    return False


def _test_dispatch_neither_set(my_predbat):
    """No URL and no sensors → returns ({}, {}) without errors."""
    base = MockFutureRateBase()
    future = FutureRate(base)

    mdata_import, mdata_export = future.futurerate_analysis({}, {})
    if mdata_import or mdata_export:
        print("ERROR: Expected ({{}}, {{}}) with no URL or sensor configured, got import={}, export={}".format(mdata_import, mdata_export))
        return True
    return False


def _test_dispatch_sensors_override_url_per_side(my_predbat):
    """Sensors override URL per side; non-overridden side falls back to URL."""
    base = MockFutureRateBase()
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_adjust_import"] = True
    base.args["futurerate_adjust_export"] = True
    base.args["futurerate_sensor_import"] = IMPORT_SENSOR
    base.args["futurerate_sensor_export"] = EXPORT_SENSOR
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    future = FutureRate(base)

    url_import = {0: 100.0}
    url_export = {0: 5.5}
    sensor_import = {0: 18.0}
    sensor_export = {0: 9.0}

    def mock_sensor(side):
        return sensor_import if side == "import" else sensor_export

    with patch.object(future, "futurerate_analysis_new", return_value=(url_import, url_export)), patch.object(future, "futurerate_analysis_sensor", side_effect=mock_sensor):
        result_import, result_export = future.futurerate_analysis({}, {})
    if result_import != sensor_import:
        print("ERROR: Expected import overridden by sensor, got {}".format(result_import))
        return True
    if result_export != sensor_export:
        print("ERROR: Expected export overridden by sensor, got {}".format(result_export))
        return True
    return False


def _test_dispatch_only_export_sensor_keeps_url_import(my_predbat):
    """Only export sensor set + URL → URL provides import, sensor provides export."""
    base = MockFutureRateBase()
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_adjust_import"] = True
    base.args["futurerate_adjust_export"] = True
    base.args["futurerate_sensor_export"] = EXPORT_SENSOR
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    future = FutureRate(base)

    url_import = {0: 21.0}
    url_export = {0: 4.0}
    sensor_export = {0: 11.0}

    with patch.object(future, "futurerate_analysis_new", return_value=(url_import, url_export)), patch.object(future, "futurerate_analysis_sensor", return_value=sensor_export):
        result_import, result_export = future.futurerate_analysis({}, {})
    if result_import != url_import:
        print("ERROR: Expected URL import preserved when only export sensor set, got {}".format(result_import))
        return True
    if result_export != sensor_export:
        print("ERROR: Expected export sensor to override URL export, got {}".format(result_export))
        return True
    return False


def _test_dispatch_sensor_empty_falls_back_to_url(my_predbat):
    """Sensor returns empty for one side → URL value for that side is preserved."""
    base = MockFutureRateBase()
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_adjust_import"] = True
    base.args["futurerate_adjust_export"] = True
    base.args["futurerate_sensor_import"] = IMPORT_SENSOR
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    future = FutureRate(base)

    url_import = {0: 21.0}
    url_export = {0: 4.0}

    with patch.object(future, "futurerate_analysis_new", return_value=(url_import, url_export)), patch.object(future, "futurerate_analysis_sensor", return_value={}):
        result_import, result_export = future.futurerate_analysis({}, {})
    if result_import != url_import:
        print("ERROR: Expected URL import preserved when sensor returns empty, got {}".format(result_import))
        return True
    if result_export != url_export:
        print("ERROR: Expected URL export preserved, got {}".format(result_export))
        return True
    return False


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------


_SUBTESTS = [
    ("sensor_import_basic", _test_sensor_import_basic),
    ("sensor_export_basic", _test_sensor_export_basic),
    ("sensor_custom_keys", _test_sensor_custom_keys),
    ("sensor_calibrate_not_called", _test_sensor_calibrate_not_called),
    ("sensor_30min_slots_fill_gap", _test_sensor_30min_slots_fill_gap),
    ("sensor_15min_slots_derived", _test_sensor_15min_slots_derived),
    ("sensor_missing_entity", _test_sensor_missing_entity),
    ("sensor_empty_prices", _test_sensor_empty_prices),
    ("sensor_skips_malformed_items", _test_sensor_skips_malformed_items),
    ("sensor_all_items_unparseable_warns", _test_sensor_all_items_unparseable_warns),
    ("dispatch_sensors_only_no_url", _test_dispatch_sensors_only_no_url),
    ("dispatch_neither_set", _test_dispatch_neither_set),
    ("dispatch_sensors_override_url_per_side", _test_dispatch_sensors_override_url_per_side),
    ("dispatch_only_export_sensor_keeps_url_import", _test_dispatch_only_export_sensor_keeps_url_import),
    ("dispatch_sensor_empty_falls_back_to_url", _test_dispatch_sensor_empty_falls_back_to_url),
]


def test_futurerate_sensor(my_predbat=None):
    """Run all futurerate_sensor unit tests."""
    print("**** Running futurerate_sensor tests ****")
    failed = False
    for name, fn in _SUBTESTS:
        result = fn(my_predbat)
        if result:
            print("  FAIL: {}".format(name))
            failed = True
        else:
            print("  PASS: {}".format(name))
    return failed
