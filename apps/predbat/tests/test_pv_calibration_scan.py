# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for SolarAPI.pv_calibration_scan (history turned into per-slot day evidence) and pv_calibration_settings."""

from datetime import timedelta

from tests.test_pv_calibration_clipping import bell
from tests.test_solcast import create_test_solar_api


def build_history(test_api, days, forecast_kw, actual_kw, pv_scaling=1.0, pv_today_from_minutes_ago=None, calls=None):
    """
    Mock the two history readers for `days` past days: the uncalibrated forecast sensor every 30 minutes (unscaled: pv_forecast_history
    applies pv_scaling) and the cumulative pv_today counter every 5 minutes. pv_today_from_minutes_ago, when
    given, cuts the pv_today history off older than that. calls, when given, records each reader's keyword arguments.
    """
    solar = test_api.solar
    base = test_api.mock_base
    solar.pv_scaling = pv_scaling
    entries = []
    start = base.now_utc_exact - timedelta(days=days + 2) + timedelta(minutes=7)
    for step in range((days + 2) * 48):
        stamp = start + timedelta(minutes=30 * step)
        minute_of_day = (stamp.hour * 60 + stamp.minute + 15) % 1440
        entries.append({"last_updated": stamp.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": str(round(forecast_kw(minute_of_day), 4)), "attributes": {}})
    pv_today = {}
    for day in range(1, days + 1):
        midnight_ago = day * 1440 + base.minutes_now
        cumulative = 0.0
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago >= 0 and (pv_today_from_minutes_ago is None or minute_ago <= pv_today_from_minutes_ago):
                pv_today[minute_ago] = cumulative
            cumulative += actual_kw(step) * 5 / 60.0

    def mock_minute_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, required=True, **kwargs):
        """The synthetic pv_today history."""
        if calls is not None:
            calls.append(("minute_data_import_export", kwargs))
        return dict(pv_today) if key == "pv_today" else {}

    def mock_get_history(entity_id, days, required=False, **kwargs):
        """The synthetic forecast history, for the uncalibrated sensor only."""
        if calls is not None:
            calls.append(("get_history_wrapper", kwargs))
        return [entries] if entity_id.endswith("_pv_forecast_h0_uncalibrated") else []

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = mock_get_history


def check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def day_totals(record):
    """(forecast, actual) kWh summed over a day's slots."""
    return sum(f for f, _ in record["slots"].values()), sum(a for _, a in record["slots"].values())


def test_scan_builds_days():
    """Five days of a 4 kW bell at 90% become five evidence days with every slot, the ratio and the measured peak."""
    test_api = create_test_solar_api()
    failed = False
    try:
        build_history(test_api, 5, bell(4.0), lambda m: 0.9 * bell(4.0)(m))
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(5)
        failed |= check("five days", evidence is not None and len(evidence) == 5, evidence and sorted(evidence))
        slots = 1440 // test_api.solar.plan_interval_minutes
        for day, record in (evidence or {}).items():
            forecast, actual = day_totals(record)
            failed |= check("{} slots".format(day), len(record["slots"]) == slots, len(record["slots"]))
            failed |= check("{} ratio".format(day), forecast > 0 and abs(actual / forecast - 0.9) < 0.01, (actual, forecast))
            failed |= check("{} peak".format(day), abs(record["peak_kw"] - 3.6) < 0.05, record["peak_kw"])
    finally:
        test_api.cleanup()
    return failed


def test_scan_forecast_before_pv_scaling():
    """The stored forecast is the provider's figure: pv_forecast_history's pv_scaling is taken back out."""
    test_api = create_test_solar_api()
    failed = False
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0))
        with test_api.patch_now_utc_exact():
            plain = test_api.solar.pv_calibration_scan(3)
        build_history(test_api, 3, bell(4.0), bell(4.0), pv_scaling=2.0)
        with test_api.patch_now_utc_exact():
            scaled = test_api.solar.pv_calibration_scan(3)
        for day in plain:
            failed |= check("{} unscaled".format(day), abs(day_totals(scaled[day])[0] - day_totals(plain[day])[0]) < 0.01, (day_totals(scaled[day])[0], day_totals(plain[day])[0]))
    finally:
        test_api.cleanup()
    return failed


def test_scan_pv_scaling_zero_stores_nothing():
    """With pv_scaling 0 there is no usable forecast to store."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0), pv_scaling=0.0)
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("pv_scaling 0", evidence == {}, evidence)


def test_scan_skips_partially_covered_day():
    """A day the pv_today history only reaches part of is not stored; the full days after it are."""
    test_api = create_test_solar_api()
    try:
        base = test_api.mock_base
        build_history(test_api, 3, bell(4.0), bell(4.0), pv_today_from_minutes_ago=2 * 1440 + base.minutes_now + 12 * 60)  # from 12:00 three days ago
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("partial day", evidence is not None and len(evidence) == 2, evidence and sorted(evidence))


def test_scan_tracked_false_passed():
    """A long read passes tracked=False to both history readers."""
    test_api = create_test_solar_api()
    calls = []
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0), calls=calls)
        with test_api.patch_now_utc_exact():
            test_api.solar.pv_calibration_scan(3, tracked=False)
    finally:
        test_api.cleanup()
    readers = {name for name, kwargs in calls if kwargs.get("tracked") is False}
    return check("tracked", readers == {"minute_data_import_export", "get_history_wrapper"}, calls)


def test_scan_history_missing_returns_none():
    """No pv_today history means the read failed: None, so the stored evidence is left alone."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0))
        test_api.mock_base.minute_data_import_export = lambda *args, **kwargs: {}
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("missing history", evidence is None, evidence)


def test_settings_defaults_and_overrides():
    """Defaults apply when apps.yaml sets nothing; a set value is used and listed as overridden."""
    test_api = create_test_solar_api()
    failed = False
    try:
        settings = test_api.solar.pv_calibration_settings()
        failed |= check("defaults", (settings["history_days"], settings["scan_days"], settings["half_life"], settings["shrink"], settings["shrink_slot"]) == (28, 2, 3.5, 0.20, 0.20), settings)
        failed |= check("no overrides", settings["overridden"] == set(), settings["overridden"])
        test_api.mock_base.mock_config = {"pv_calibration_shrink": 0.3}
        settings = test_api.solar.pv_calibration_settings()
        failed |= check("override", settings["shrink"] == 0.3 and "shrink" in settings["overridden"], settings)
    finally:
        test_api.cleanup()
    return failed


def test_settings_clamp_and_warn():
    """scan_days is held to history_days, under 3 history days warns, and an invalid value falls back with a warning."""
    test_api = create_test_solar_api()
    logged = []
    failed = False
    try:
        test_api.solar.log = lambda message: logged.append(message)
        test_api.mock_base.mock_config = {"pv_calibration_history_days": 2, "pv_calibration_scan_days": 5, "pv_calibration_half_life": -1}
        settings = test_api.solar.pv_calibration_settings()
        failed |= check("clamp", settings["scan_days"] == 2 and settings["history_days"] == 2, settings)
        failed |= check("half_life fallback", settings["half_life"] == 3.5, settings)
        failed |= check("warn 3 days", any(message.startswith("Warn:") and "3 days" in message for message in logged), logged)
        failed |= check("warn invalid", any(message.startswith("Warn:") and "pv_calibration_half_life" in message for message in logged), logged)
    finally:
        test_api.cleanup()
    return failed


def run_pv_calibration_scan_tests(my_predbat=None):
    """Run the PV calibration history scan and settings tests."""
    failed = False
    for test in (
        test_scan_builds_days,
        test_scan_forecast_before_pv_scaling,
        test_scan_pv_scaling_zero_stores_nothing,
        test_scan_skips_partially_covered_day,
        test_scan_tracked_false_passed,
        test_scan_history_missing_returns_none,
        test_settings_defaults_and_overrides,
        test_settings_clamp_and_warn,
    ):
        print("**** {} ****".format(test.__name__))
        failed = test() or failed
    return failed
