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
empty sensor data, calibration plumbing, sensor-only setups (no URL), and the override
relationship between sensor and URL.
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


def _make_sensor_future(base, import_entity=IMPORT_SENSOR, export_entity=None, bypass_calibrate=False):
    """Construct a FutureRate wired up with one or both sensor sources.

    When ``bypass_calibrate`` is True, replace ``futurerate_calibrate`` with a
    passthrough so tests can assert raw ingest values without the default
    Nordpool-shaped multiplier/VAT corrections being applied.
    """
    if import_entity:
        base.args["futurerate_sensor_import"] = import_entity
    if export_entity:
        base.args["futurerate_sensor_export"] = export_entity
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    future = FutureRate(base)
    if bypass_calibrate:
        future.futurerate_calibrate = lambda real_mdata, mdata, is_import, peak_start_minutes, peak_end_minutes: mdata
    return future


# ---------------------------------------------------------------------------
# Sensor branch: happy paths
# ---------------------------------------------------------------------------


def _test_sensor_import_basic(my_predbat):
    """Import-side sensor with AgilePredict-shaped data populates mdata in p/kWh."""
    base = MockFutureRateBase()
    prices = _make_prices(base, hours=2, rate=15.0)
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base, bypass_calibrate=True)

    mdata = future.futurerate_analysis_sensor({}, "import")

    if not mdata or mdata.get(0) != 15.0:
        print("ERROR: Expected import sensor mdata[0] == 15.0, got {}".format(mdata.get(0) if mdata else mdata))
        return True
    return False


def _test_sensor_export_basic(my_predbat):
    """Export-side sensor populates export-side mdata using the same shape options."""
    base = MockFutureRateBase()
    prices = _make_prices(base, hours=2, rate=8.5)
    base.set_entity_attr(EXPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base, import_entity=None, export_entity=EXPORT_SENSOR, bypass_calibrate=True)

    mdata = future.futurerate_analysis_sensor({}, "export")

    if not mdata or mdata.get(0) != 8.5:
        print("ERROR: Expected export sensor mdata[0] == 8.5, got {}".format(mdata.get(0) if mdata else mdata))
        return True
    return False


def _test_sensor_custom_keys(my_predbat):
    """Custom attribute / time / rate keys are honoured for both sides."""
    base = MockFutureRateBase()
    base.args["futurerate_sensor_attribute"] = "forecasts"
    base.args["futurerate_sensor_time_key"] = "ts"
    base.args["futurerate_sensor_rate_key"] = "p"
    prices = _make_prices(base, hours=2, rate=22.0, key_time="ts", key_rate="p")
    base.set_entity_attr(IMPORT_SENSOR, "forecasts", prices)
    future = _make_sensor_future(base, bypass_calibrate=True)

    mdata = future.futurerate_analysis_sensor({}, "import")

    if mdata.get(0) != 22.0:
        print("ERROR: Expected import mdata[0] == 22.0 with custom keys, got {}".format(mdata.get(0)))
        return True
    return False


# ---------------------------------------------------------------------------
# Sensor branch: degenerate inputs
# ---------------------------------------------------------------------------


def _test_sensor_missing_entity(my_predbat):
    """Sensor entity has no state at all → returns {} and flags status."""
    base = MockFutureRateBase()
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor({}, "import")

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

    mdata = future.futurerate_analysis_sensor({}, "import")

    if mdata:
        print("ERROR: Expected empty dict for empty prices list, got {}".format(mdata))
        return True
    return False


def _test_sensor_skips_malformed_items(my_predbat):
    """Items missing keys, with bad timestamps, or non-numeric rates are skipped."""
    base = MockFutureRateBase()
    good_ts = base.midnight_utc.isoformat()
    prices = [
        {"date_time": good_ts, "agile_pred": 17.5},
        {"date_time": "not-a-timestamp", "agile_pred": 10.0},
        {"date_time": good_ts},
        {"agile_pred": 12.0},
        {"date_time": good_ts, "agile_pred": "not-a-number"},
        "scalar-not-a-dict",
    ]
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base, bypass_calibrate=True)

    mdata = future.futurerate_analysis_sensor({}, "import")

    if mdata.get(0) != 17.5:
        print("ERROR: Expected only the well-formed item to populate mdata[0]==17.5, got {}".format(mdata.get(0)))
        return True
    return False


# ---------------------------------------------------------------------------
# Calibration plumbing
# ---------------------------------------------------------------------------


def _test_sensor_import_calibration_plumbing(my_predbat):
    """futurerate_adjust_import=True passes real import rates to calibrate with is_import=True."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_import"] = True
    prices = _make_prices(base, hours=2, rate=20.0)
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    calls = []
    future.futurerate_calibrate = lambda real_mdata, mdata, is_import, **kw: (calls.append({"real_mdata": real_mdata, "is_import": is_import}) or mdata)

    real_import = {0: 30.0}
    future.futurerate_analysis_sensor(real_import, "import")

    if not calls or not calls[0]["is_import"] or calls[0]["real_mdata"] != real_import:
        print("ERROR: import side should call calibrate with is_import=True and real import rates, got {}".format(calls))
        return True
    return False


def _test_sensor_export_calibration_plumbing(my_predbat):
    """futurerate_adjust_export=True passes real export rates to calibrate with is_import=False."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_export"] = True
    prices = _make_prices(base, hours=2, rate=8.0)
    base.set_entity_attr(EXPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base, import_entity=None, export_entity=EXPORT_SENSOR)

    calls = []
    future.futurerate_calibrate = lambda real_mdata, mdata, is_import, **kw: (calls.append({"real_mdata": real_mdata, "is_import": is_import}) or mdata)

    real_export = {0: 5.5}
    future.futurerate_analysis_sensor(real_export, "export")

    if not calls or calls[0]["is_import"] or calls[0]["real_mdata"] != real_export:
        print("ERROR: export side should call calibrate with is_import=False and real export rates, got {}".format(calls))
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
    future = _make_sensor_future(base, export_entity=EXPORT_SENSOR, bypass_calibrate=True)

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

    def mock_sensor(real_rates, side):
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
    ("sensor_missing_entity", _test_sensor_missing_entity),
    ("sensor_empty_prices", _test_sensor_empty_prices),
    ("sensor_skips_malformed_items", _test_sensor_skips_malformed_items),
    ("sensor_import_calibration_plumbing", _test_sensor_import_calibration_plumbing),
    ("sensor_export_calibration_plumbing", _test_sensor_export_calibration_plumbing),
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
