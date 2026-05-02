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
sensor and URL, variable slot durations derived from the feed itself, the guarantee
that sensor data is NOT run through Nordpool calibration, and an end-to-end check
that sensor data reaches the planner via rate_replicate without futurerate_adjust_*
flags being set.
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
# End-to-end through the planner: sensor data must drive rate_replicate
# without any futurerate_adjust_* flag being set. This is the test that
# would have caught the bug where futurerate_analysis_sensor populated
# future_energy_rates_import but fetch.rate_replicate ignored it.
# ---------------------------------------------------------------------------


def _test_sensor_drives_rate_replicate_without_adjust_flags(my_predbat):
    """End-to-end: sensor-only setup with no futurerate_adjust_* flags must still feed future_energy_rates_import/export into rate_replicate.

    This drives the *full* pipeline: registers sensor state on the test HA interface,
    constructs FutureRate(my_predbat), calls futurerate_analysis() (which is what
    fetch.py:917-918 does in production), then calls rate_replicate. Asserts both
    the sensor->dict population AND the activation flag plumbing AND the gate in
    rate_replicate. A regression in any of those layers will fail this test.
    """
    if my_predbat is None or not getattr(my_predbat, "ha_interface", None):
        # No real PredBat or no test HA interface — skip cleanly.
        return False

    from futurerate import FutureRate

    saved_args = dict(my_predbat.args)
    saved_midnight = my_predbat.midnight
    saved_midnight_utc = my_predbat.midnight_utc
    saved_now_utc = my_predbat.now_utc
    saved_minutes_now = my_predbat.minutes_now
    saved_forecast_minutes = my_predbat.forecast_minutes
    saved_offsets = (my_predbat.metric_future_rate_offset_import, my_predbat.metric_future_rate_offset_export)
    saved_future_import = my_predbat.future_energy_rates_import
    saved_future_export = my_predbat.future_energy_rates_export
    saved_rate_max = my_predbat.rate_max
    saved_active_import = getattr(my_predbat, "future_rates_active_import", None)
    saved_active_export = getattr(my_predbat, "future_rates_active_export", None)
    saved_dummy_import = my_predbat.ha_interface.dummy_items.get(IMPORT_SENSOR)
    saved_dummy_export = my_predbat.ha_interface.dummy_items.get(EXPORT_SENSOR)

    try:
        # Clear adjust flags — we're testing that sensor data activates without them.
        my_predbat.args.pop("futurerate_adjust_import", None)
        my_predbat.args.pop("futurerate_adjust_export", None)
        my_predbat.args.pop("futurerate_adjust_auto", None)
        my_predbat.args.pop("futurerate_url", None)
        my_predbat.args["futurerate_sensor_import"] = IMPORT_SENSOR
        my_predbat.args["futurerate_sensor_export"] = EXPORT_SENSOR

        # Time anchor used by FutureRate — keep it consistent so both sensor parsing
        # and rate_replicate see the same view of "tomorrow at minute 1440+600".
        my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        my_predbat.midnight = my_predbat.midnight_utc.replace(tzinfo=None)
        my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
        my_predbat.forecast_minutes = 2880
        my_predbat.metric_future_rate_offset_import = 0
        my_predbat.metric_future_rate_offset_export = 0
        my_predbat.rate_max = 99.0

        # Register sensor state and the AgilePredict-shaped attribute on the test HA
        # interface. 30-minute slots covering today + tomorrow (96 × 30min) at 22.0p
        # import / 8.0p export — this mirrors AgilePredict's typical ~48h forward
        # publication. Tomorrow-only feeds (the case where today's data is absent)
        # are exercised separately by _test_sensor_tomorrow_only_feed_drives_planner.
        import_prices = []
        export_prices = []
        for slot in range(96):  # 96 × 30min = 48h
            ts = (my_predbat.midnight_utc + timedelta(minutes=slot * 30)).isoformat()
            import_prices.append({"date_time": ts, "agile_pred": 22.0})
            export_prices.append({"date_time": ts, "agile_pred": 8.0})
        my_predbat.ha_interface.set_state(IMPORT_SENSOR, "ok", attributes={"prices": import_prices})
        my_predbat.ha_interface.set_state(EXPORT_SENSOR, "ok", attributes={"prices": export_prices})

        # Drive the dispatcher exactly like fetch.py:917-918 does.
        future_rate = FutureRate(my_predbat)
        my_predbat.future_energy_rates_import, my_predbat.future_energy_rates_export = future_rate.futurerate_analysis({}, {})

        if not my_predbat.future_energy_rates_import:
            print("ERROR: futurerate_analysis returned no import data from sensor; check sensor wiring / parsing")
            return True
        if not getattr(my_predbat, "future_rates_active_import", False):
            print("ERROR: future_rates_active_import was not set True by dispatcher despite sensor producing data")
            return True
        if not getattr(my_predbat, "future_rates_active_export", False):
            print("ERROR: future_rates_active_export was not set True by dispatcher despite sensor producing data")
            return True

        # Now exercise the gate in rate_replicate.
        rates_import = {minute: 15.0 for minute in range(0, 1440)}
        rates_export = {minute: 5.0 for minute in range(0, 1440)}
        result_import, replicated_import = my_predbat.rate_replicate(rates_import, is_import=True, is_gas=False)
        result_export, replicated_export = my_predbat.rate_replicate(rates_export, is_import=False, is_gas=False)

        probe = 1440 + 600  # tomorrow at 10:00
        if result_import.get(probe) != 22.0:
            print("ERROR: import sensor data was ignored by rate_replicate without futurerate_adjust_import; expected 22.0 at minute {}, got {}".format(probe, result_import.get(probe)))
            return True
        if replicated_import.get(probe) != "future":
            print("ERROR: replicated_import[{}] should be 'future' (sensor-derived), got {}".format(probe, replicated_import.get(probe)))
            return True
        if result_export.get(probe) != 8.0:
            print("ERROR: export sensor data was ignored by rate_replicate without futurerate_adjust_export; expected 8.0 at minute {}, got {}".format(probe, result_export.get(probe)))
            return True
        if replicated_export.get(probe) != "future":
            print("ERROR: replicated_export[{}] should be 'future' (sensor-derived), got {}".format(probe, replicated_export.get(probe)))
            return True
        return False
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        my_predbat.midnight = saved_midnight
        my_predbat.midnight_utc = saved_midnight_utc
        my_predbat.now_utc = saved_now_utc
        my_predbat.minutes_now = saved_minutes_now
        my_predbat.forecast_minutes = saved_forecast_minutes
        my_predbat.metric_future_rate_offset_import, my_predbat.metric_future_rate_offset_export = saved_offsets
        my_predbat.future_energy_rates_import = saved_future_import
        my_predbat.future_energy_rates_export = saved_future_export
        my_predbat.rate_max = saved_rate_max
        if saved_active_import is None:
            my_predbat.future_rates_active_import = False
        else:
            my_predbat.future_rates_active_import = saved_active_import
        if saved_active_export is None:
            my_predbat.future_rates_active_export = False
        else:
            my_predbat.future_rates_active_export = saved_active_export
        if saved_dummy_import is None:
            my_predbat.ha_interface.dummy_items.pop(IMPORT_SENSOR, None)
        else:
            my_predbat.ha_interface.dummy_items[IMPORT_SENSOR] = saved_dummy_import
        if saved_dummy_export is None:
            my_predbat.ha_interface.dummy_items.pop(EXPORT_SENSOR, None)
        else:
            my_predbat.ha_interface.dummy_items[EXPORT_SENSOR] = saved_dummy_export


# ---------------------------------------------------------------------------
# Regression: a typo'd futurerate_sensor_<side> entity must NOT silently
# activate the planner's future-rate gate when the URL+adjust on the OTHER
# side has populated this side's dict as a side effect.
# ---------------------------------------------------------------------------


def _test_sensor_tomorrow_only_feed_drives_planner(my_predbat):
    """Sensor feed that only publishes tomorrow's prices (no today coverage) must still drive rate_replicate.

    Real-world AgilePredict sometimes only publishes the next 24h forward; the
    historic `minute_mod in future_energy_rates_*` gate in rate_replicate would
    silently ignore such a feed even though futurerate_analysis_sensor parsed it
    successfully. This test seeds only minutes 1440-2879 (tomorrow) and asserts
    the planner picks them up.
    """
    if my_predbat is None or not getattr(my_predbat, "ha_interface", None):
        return False

    from futurerate import FutureRate

    saved_args = dict(my_predbat.args)
    saved_midnight = my_predbat.midnight
    saved_midnight_utc = my_predbat.midnight_utc
    saved_now_utc = my_predbat.now_utc
    saved_minutes_now = my_predbat.minutes_now
    saved_forecast_minutes = my_predbat.forecast_minutes
    saved_offsets = (my_predbat.metric_future_rate_offset_import, my_predbat.metric_future_rate_offset_export)
    saved_future_import = my_predbat.future_energy_rates_import
    saved_future_export = my_predbat.future_energy_rates_export
    saved_rate_max = my_predbat.rate_max
    saved_active_import = getattr(my_predbat, "future_rates_active_import", None)
    saved_active_export = getattr(my_predbat, "future_rates_active_export", None)
    saved_dummy_import = my_predbat.ha_interface.dummy_items.get(IMPORT_SENSOR)

    try:
        my_predbat.args.pop("futurerate_adjust_import", None)
        my_predbat.args.pop("futurerate_adjust_export", None)
        my_predbat.args.pop("futurerate_adjust_auto", None)
        my_predbat.args.pop("futurerate_url", None)
        my_predbat.args["futurerate_sensor_import"] = IMPORT_SENSOR
        my_predbat.args.pop("futurerate_sensor_export", None)

        my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        my_predbat.midnight = my_predbat.midnight_utc.replace(tzinfo=None)
        my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
        my_predbat.forecast_minutes = 2880
        my_predbat.metric_future_rate_offset_import = 0
        my_predbat.metric_future_rate_offset_export = 0
        my_predbat.rate_max = 99.0

        # Tomorrow-only feed: 48 × 30min slots starting from tomorrow midnight.
        # Today is intentionally absent — this is the case real AgilePredict can
        # produce when only the next ~24h forward is published.
        tomorrow_midnight = my_predbat.midnight_utc + timedelta(days=1)
        prices = []
        for slot in range(48):
            ts = (tomorrow_midnight + timedelta(minutes=slot * 30)).isoformat()
            prices.append({"date_time": ts, "agile_pred": 19.5})
        my_predbat.ha_interface.set_state(IMPORT_SENSOR, "ok", attributes={"prices": prices})

        future_rate = FutureRate(my_predbat)
        my_predbat.future_energy_rates_import, my_predbat.future_energy_rates_export = future_rate.futurerate_analysis({}, {})

        # Sanity-check: dispatcher populated only tomorrow's minutes.
        if 600 in my_predbat.future_energy_rates_import:
            print("ERROR: test setup expected today (minute 600) to be absent from dict, found {}".format(my_predbat.future_energy_rates_import.get(600)))
            return True
        if 1440 + 600 not in my_predbat.future_energy_rates_import:
            print("ERROR: test setup expected tomorrow (minute 2040) populated, was missing")
            return True

        rates_import = {minute: 12.0 for minute in range(0, 1440)}
        result_import, replicated_import = my_predbat.rate_replicate(rates_import, is_import=True, is_gas=False)

        probe = 1440 + 600  # tomorrow at 10:00
        if result_import.get(probe) != 19.5:
            print("ERROR: tomorrow-only sensor feed was ignored by rate_replicate (expected 19.5 at minute {}, got {}); minute_mod gate likely still active".format(probe, result_import.get(probe)))
            return True
        if replicated_import.get(probe) != "future":
            print("ERROR: replicated[{}] should be 'future', got {}".format(probe, replicated_import.get(probe)))
            return True
        return False
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        my_predbat.midnight = saved_midnight
        my_predbat.midnight_utc = saved_midnight_utc
        my_predbat.now_utc = saved_now_utc
        my_predbat.minutes_now = saved_minutes_now
        my_predbat.forecast_minutes = saved_forecast_minutes
        my_predbat.metric_future_rate_offset_import, my_predbat.metric_future_rate_offset_export = saved_offsets
        my_predbat.future_energy_rates_import = saved_future_import
        my_predbat.future_energy_rates_export = saved_future_export
        my_predbat.rate_max = saved_rate_max
        my_predbat.future_rates_active_import = False if saved_active_import is None else saved_active_import
        my_predbat.future_rates_active_export = False if saved_active_export is None else saved_active_export
        if saved_dummy_import is None:
            my_predbat.ha_interface.dummy_items.pop(IMPORT_SENSOR, None)
        else:
            my_predbat.ha_interface.dummy_items[IMPORT_SENSOR] = saved_dummy_import


def _test_sensor_z_suffix_iso_timestamps(my_predbat=None):
    """Timestamps with a 'Z' zulu suffix (AgilePredict-shaped UTC) must parse on Python 3.10 and 3.11+."""
    base = MockFutureRateBase()
    z_ts = base.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    z_ts_next = (base.midnight_utc + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    prices = [
        {"date_time": z_ts, "agile_pred": 16.0},
        {"date_time": z_ts_next, "agile_pred": 17.0},
    ]
    base.set_entity_attr(IMPORT_SENSOR, "prices", prices)
    future = _make_sensor_future(base)

    mdata = future.futurerate_analysis_sensor("import")

    if mdata.get(0) != 16.0:
        print("ERROR: Expected Z-suffix UTC timestamp to parse and produce mdata[0]==16.0, got {}".format(mdata.get(0)))
        return True
    if mdata.get(30) != 17.0:
        print("ERROR: Expected Z-suffix second slot to parse and produce mdata[30]==17.0, got {}".format(mdata.get(30)))
        return True
    return False


def _test_typo_sensor_with_adjust_uses_url_fallback(my_predbat=None):
    """Documented contract: URL+adjust_export=True+sensor_export=typo → sensor returns empty, URL is used as fallback (the docs explicitly call this out)."""
    base = MockFutureRateBase()
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_adjust_import"] = False
    base.args["futurerate_adjust_export"] = True  # user explicitly opted INTO URL on export
    base.args["futurerate_sensor_export"] = "sensor.typo_does_not_exist"  # tries to override but fails
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"

    future = FutureRate(base)
    url_import = {0: 21.0}
    url_export = {0: 4.0}
    with patch.object(future, "futurerate_analysis_new", return_value=(url_import, url_export)):
        mdata_import, mdata_export = future.futurerate_analysis({}, {})

    if getattr(base, "future_rates_active_import", None):
        print("ERROR: import side should not activate (adjust_import=False, no sensor_import set)")
        return True
    if not getattr(base, "future_rates_active_export", False):
        print("ERROR: export side should activate via adjust_export=True even if sensor_export typo'd — URL is the documented fallback when sensor returns empty")
        return True
    if mdata_export != url_export:
        print("ERROR: export should fall back to URL data when sensor returns empty, got {}".format(mdata_export))
        return True
    return False


def _test_typo_sensor_does_not_self_activate(my_predbat=None):
    """If futurerate_sensor_export is set but the entity returns no data, the export gate must stay closed even when URL fetched both sides."""
    base = MockFutureRateBase()
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_adjust_import"] = True
    base.args["futurerate_adjust_export"] = False  # user explicitly opted OUT of export
    base.args["futurerate_sensor_export"] = "sensor.typo_does_not_exist"
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"

    future = FutureRate(base)
    # URL fetches both sides (because adjust_import is True). Sensor export returns
    # empty (entity not registered). Without per-side activation flags the export
    # gate would open on the leftover URL data.
    url_import = {0: 21.0}
    url_export = {0: 4.0}
    with patch.object(future, "futurerate_analysis_new", return_value=(url_import, url_export)):
        future.futurerate_analysis({}, {})

    if not getattr(base, "future_rates_active_import", False):
        print("ERROR: import side should have activated (adjust_import=True with URL data)")
        return True
    if getattr(base, "future_rates_active_export", False):
        print("ERROR: export side must NOT activate when adjust_export=False and sensor returned empty (would silently use URL export data)")
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
    ("sensor_drives_rate_replicate_without_adjust_flags", _test_sensor_drives_rate_replicate_without_adjust_flags),
    ("sensor_tomorrow_only_feed_drives_planner", _test_sensor_tomorrow_only_feed_drives_planner),
    ("sensor_z_suffix_iso_timestamps", _test_sensor_z_suffix_iso_timestamps),
    ("typo_sensor_with_adjust_uses_url_fallback", _test_typo_sensor_with_adjust_uses_url_fallback),
    ("typo_sensor_does_not_self_activate", _test_typo_sensor_does_not_self_activate),
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
