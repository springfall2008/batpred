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
Tests for Agile tariff auto-detection and the futurerate_adjust_auto feature.

These tests cover detection of whether configured import/export entities are
on Agile tariffs, along with futurerate_adjust_auto behavior.

futurerate_adjust_auto: when True, auto-detects which of import/export rates
should be calibrated against actual Agile rates (as opposed to the manual
futurerate_adjust_import / futurerate_adjust_export flags).
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from futurerate import FutureRate
from const import TIME_FORMAT


# ---------------------------------------------------------------------------
# Shared mock infrastructure
# ---------------------------------------------------------------------------

NORDPOOL_URL = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices?date=DATE&market=N2EX_DayAhead&deliveryArea=UK&currency=GBP"
IMPORT_ENTITY = "sensor.octopus_import"
EXPORT_ENTITY = "sensor.octopus_export"


class MockFutureRateBase:
    """Minimal base object that satisfies all FutureRate.__init__ delegations."""

    def __init__(self):
        self.args = {}
        self.log_messages = []
        self.plan_interval_minutes = 30
        now_utc = datetime.now(timezone.utc)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight = self.midnight_utc.replace(tzinfo=None)
        self.forecast_days = 2
        self.minutes_now = 0
        self.forecast_plan_hours = 48
        self.futurerate_url_cache = {}
        self._state_store = {}

    def log(self, message):
        """Store log messages for assertion."""
        self.log_messages.append(message)

    def record_status(self, message, debug=None, had_errors=False):
        """No-op status recorder."""

    def get_arg(self, arg, default=None, indirect=False):
        """Return value from args dict or the supplied default."""
        return self.args.get(arg, default)

    def set_arg(self, arg, value):
        """Helper to set args for tests."""
        self.args[arg] = value

    def time_abs_str(self, minutes):
        """Convert minutes-since-midnight to HH:MM string."""
        return "{:02d}:{:02d}".format(int(minutes / 60), int(minutes % 60))

    def get_state_wrapper(self, entity_id, default=None, attribute=None):
        """Mock state wrapper backed by _state_store dict."""
        if entity_id not in self._state_store:
            return default
        if attribute:
            return self._state_store[entity_id].get(attribute, default)
        return self._state_store[entity_id].get("state", default)

    def set_entity_attr(self, entity_id, attribute, value):
        """Helper to set entity attributes for tests."""
        if entity_id not in self._state_store:
            self._state_store[entity_id] = {}
        self._state_store[entity_id][attribute] = value


def _make_nordpool_data(base, hours=2):
    """Return a minimal multi-area entries payload for *hours* slots starting at midnight UTC."""
    entries = []
    for h in range(hours):
        t_start = base.midnight_utc + timedelta(hours=h)
        t_end = t_start + timedelta(hours=1)
        entries.append(
            {
                "deliveryStart": t_start.strftime(TIME_FORMAT),
                "deliveryEnd": t_end.strftime(TIME_FORMAT),
                "entryPerArea": {"UK": 50.0},
            }
        )
    return {"multiAreaEntries": entries}


def _make_future_rate(base):
    """Construct a FutureRate instance backed by base and configure a Nordpool URL."""
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    return FutureRate(base)


# ---------------------------------------------------------------------------
# import_export_is_agile() tests
# ---------------------------------------------------------------------------


def _test_import_is_agile_via_tariff(my_predbat):
    """Import entity reports 'AGILE-24-10-01' in the tariff attribute."""
    base = MockFutureRateBase()
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "AGILE-24-10-01")
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if not import_agile:
        print("ERROR: Expected import_agile=True when tariff contains 'agile', got False")
        return True
    if export_agile:
        print("ERROR: Expected export_agile=False, got True")
        return True
    return False


def _test_import_is_agile_via_tariff_code(my_predbat):
    """Import entity has no 'tariff' attribute but 'tariff_code' contains 'agile'."""
    base = MockFutureRateBase()
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    # tariff attribute absent (returns None), tariff_code present
    base.set_entity_attr(IMPORT_ENTITY, "tariff_code", "E-1R-AGILE-24-10-01-A")
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if not import_agile:
        print("ERROR: Expected import_agile=True via tariff_code fallback, got False")
        return True
    if export_agile:
        print("ERROR: Expected export_agile=False, got True")
        return True
    return False


def _test_export_is_agile(my_predbat):
    """Export entity reports an Agile tariff; import is non-Agile."""
    base = MockFutureRateBase()
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.args["metric_octopus_export"] = EXPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "FLUX-IMPORT-23-02-14")
    base.set_entity_attr(EXPORT_ENTITY, "tariff", "AGILE-OUTGOING-BB-23-02-28")
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if import_agile:
        print("ERROR: Expected import_agile=False for non-Agile tariff, got True")
        return True
    if not export_agile:
        print("ERROR: Expected export_agile=True, got False")
        return True
    return False


def _test_both_are_agile(my_predbat):
    """Both import and export have Agile tariffs."""
    base = MockFutureRateBase()
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.args["metric_octopus_export"] = EXPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "AGILE-24-10-01")
    base.set_entity_attr(EXPORT_ENTITY, "tariff", "AGILE-OUTGOING-BB-23-02-28")
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if not import_agile:
        print("ERROR: Expected import_agile=True, got False")
        return True
    if not export_agile:
        print("ERROR: Expected export_agile=True, got False")
        return True
    return False


def _test_non_agile_tariff(my_predbat):
    """Import has a non-Agile tariff (Flux); should return (False, False)."""
    base = MockFutureRateBase()
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "FLUX-IMPORT-23-02-14")
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if import_agile:
        print("ERROR: Expected import_agile=False for non-Agile tariff, got True")
        return True
    if export_agile:
        print("ERROR: Expected export_agile=False, got True")
        return True
    return False


def _test_no_entities_configured(my_predbat):
    """No Octopus entities configured; import_export_is_agile should return (False, False)."""
    base = MockFutureRateBase()
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if import_agile:
        print("ERROR: Expected import_agile=False with no entity, got True")
        return True
    if export_agile:
        print("ERROR: Expected export_agile=False with no entity, got True")
        return True
    return False


def _test_tariff_case_insensitive(my_predbat):
    """Agile detection is case-insensitive (e.g. 'Agile-24-10-01' in mixed case)."""
    base = MockFutureRateBase()
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "Agile-24-10-01")
    future = _make_future_rate(base)

    import_agile, export_agile = future.import_export_is_agile()

    if not import_agile:
        print("ERROR: Expected import_agile=True for mixed-case 'Agile' tariff, got False")
        return True
    return False


# ---------------------------------------------------------------------------
# futurerate_analysis() futurerate_auto tests (no network calls)
# ---------------------------------------------------------------------------


def _test_futurerate_auto_no_url(my_predbat):
    """When futurerate_url is not set, analysis returns empty regardless of auto flag."""
    base = MockFutureRateBase()
    base.args.pop("futurerate_url", None)
    base.args["futurerate_auto"] = True
    future = FutureRate(base)

    rate_import, rate_export = future.futurerate_analysis({}, {})

    if rate_import or rate_export:
        print("ERROR: Expected empty dicts when no URL is configured")
        return True
    return False


def _test_futurerate_auto_disabled_proceeds(my_predbat):
    """futurerate_adjust_auto=False with a manual flag set: calls futurerate_analysis_new."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = False
    base.args["futurerate_adjust_import"] = True
    future = _make_future_rate(base)

    sentinel = ({"key": 1}, {"key": 2})
    with patch.object(future, "futurerate_analysis_new", return_value=sentinel) as mock_new:
        result = future.futurerate_analysis({}, {})
        if not mock_new.called:
            print("ERROR: Expected futurerate_analysis_new to be called when futurerate_adjust_auto=False")
            return True
        if result != sentinel:
            print("ERROR: Expected sentinel result, got {}".format(result))
            return True
    return False


def _test_futurerate_auto_no_agile_returns_empty(my_predbat):
    """futurerate_adjust_auto=True + no Agile detected → return ({}, {}) without hitting the API."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = True
    future = _make_future_rate(base)

    # No entities configured -> import_export_is_agile returns (False, False)
    with patch.object(future, "futurerate_analysis_new") as mock_new:
        rate_import, rate_export = future.futurerate_analysis({}, {})
        if mock_new.called:
            print("ERROR: futurerate_analysis_new should not be called when no Agile detected")
            return True
        if rate_import or rate_export:
            print("ERROR: Expected empty dicts when no Agile detected and futurerate_adjust_auto=True")
            return True
    return False


def _test_futurerate_auto_import_agile_proceeds(my_predbat):
    """futurerate_adjust_auto=True + import is Agile → calls futurerate_analysis_new."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = True
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "AGILE-24-10-01")
    future = _make_future_rate(base)

    sentinel = ({"minute": 100}, {})
    with patch.object(future, "futurerate_analysis_new", return_value=sentinel) as mock_new:
        result = future.futurerate_analysis({}, {})
        if not mock_new.called:
            print("ERROR: Expected futurerate_analysis_new to be called when import is Agile")
            return True
        if result != sentinel:
            print("ERROR: Expected sentinel result, got {}".format(result))
            return True
    return False


def _test_futurerate_auto_export_agile_proceeds(my_predbat):
    """futurerate_adjust_auto=True + only export is Agile → calls futurerate_analysis_new."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = True
    base.args["metric_octopus_export"] = EXPORT_ENTITY
    base.set_entity_attr(EXPORT_ENTITY, "tariff", "AGILE-OUTGOING-BB-23-02-28")
    future = _make_future_rate(base)

    sentinel = ({}, {"minute": 200})
    with patch.object(future, "futurerate_analysis_new", return_value=sentinel) as mock_new:
        result = future.futurerate_analysis({}, {})
        if not mock_new.called:
            print("ERROR: Expected futurerate_analysis_new to be called when export is Agile")
            return True
        if result != sentinel:
            print("ERROR: Expected sentinel result, got {}".format(result))
            return True
    return False


# ---------------------------------------------------------------------------
# futurerate_adjust_auto tests (exercises futurerate_analysis_new internals)
# ---------------------------------------------------------------------------


def _test_futurerate_adjust_auto_import_agile_calibrates_import(my_predbat):
    """futurerate_adjust_auto=True + import Agile → calibrate import with real rates."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = True
    base.args["futurerate_adjust_import"] = False
    base.args["futurerate_adjust_export"] = False
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "AGILE-24-10-01")
    future = _make_future_rate(base)

    # Mock download to return minimal valid Nordpool data
    mock_data = _make_nordpool_data(base, hours=2)
    future.download_futurerate_data = lambda url: mock_data

    # Capture futurerate_calibrate calls
    calibrate_calls = []

    def mock_calibrate(real_mdata, mdata, is_import, peak_start_minutes, peak_end_minutes):
        calibrate_calls.append({"real_mdata": real_mdata, "is_import": is_import})
        return mdata

    future.futurerate_calibrate = mock_calibrate

    real_import_rates = {0: 15.0, 30: 16.0}
    real_export_rates = {0: 5.0, 30: 5.5}
    future.futurerate_analysis_new(NORDPOOL_URL, real_import_rates, real_export_rates)

    import_call = next((c for c in calibrate_calls if c["is_import"]), None)
    if import_call is None:
        print("ERROR: futurerate_calibrate not called for import")
        return True
    if import_call["real_mdata"] != real_import_rates:
        print("ERROR: Expected real import rates to be passed to calibrate when adjust_auto and import is Agile, got {}".format(import_call["real_mdata"]))
        return True

    export_call = next((c for c in calibrate_calls if not c["is_import"]), None)
    if export_call is None:
        print("ERROR: futurerate_calibrate not called for export")
        return True
    if export_call["real_mdata"] != {}:
        print("ERROR: Expected empty dict for export calibrate when export is not Agile, got {}".format(export_call["real_mdata"]))
        return True

    return False


def _test_futurerate_adjust_auto_export_agile_calibrates_export(my_predbat):
    """futurerate_adjust_auto=True + export Agile → calibrate export with real rates."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = True
    base.args["futurerate_adjust_import"] = False
    base.args["futurerate_adjust_export"] = False
    base.args["metric_octopus_export"] = EXPORT_ENTITY
    base.set_entity_attr(EXPORT_ENTITY, "tariff", "AGILE-OUTGOING-BB-23-02-28")
    future = _make_future_rate(base)

    mock_data = _make_nordpool_data(base, hours=2)
    future.download_futurerate_data = lambda url: mock_data

    calibrate_calls = []

    def mock_calibrate(real_mdata, mdata, is_import, peak_start_minutes, peak_end_minutes):
        calibrate_calls.append({"real_mdata": real_mdata, "is_import": is_import})
        return mdata

    future.futurerate_calibrate = mock_calibrate

    real_import_rates = {0: 15.0}
    real_export_rates = {0: 5.0}
    future.futurerate_analysis_new(NORDPOOL_URL, real_import_rates, real_export_rates)

    import_call = next((c for c in calibrate_calls if c["is_import"]), None)
    if import_call is None:
        print("ERROR: futurerate_calibrate not called for import")
        return True
    if import_call["real_mdata"] != {}:
        print("ERROR: Expected empty dict for import calibrate when import is not Agile, got {}".format(import_call["real_mdata"]))
        return True

    export_call = next((c for c in calibrate_calls if not c["is_import"]), None)
    if export_call is None:
        print("ERROR: futurerate_calibrate not called for export")
        return True
    if export_call["real_mdata"] != real_export_rates:
        print("ERROR: Expected real export rates to be passed to calibrate, got {}".format(export_call["real_mdata"]))
        return True

    return False


def _test_futurerate_adjust_auto_disabled_uses_manual_flags(my_predbat):
    """futurerate_adjust_auto=False → manual futurerate_adjust_import/export flags respected."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = False
    base.args["futurerate_adjust_import"] = True
    base.args["futurerate_adjust_export"] = False
    future = _make_future_rate(base)

    mock_data = _make_nordpool_data(base, hours=2)
    future.download_futurerate_data = lambda url: mock_data

    calibrate_calls = []

    def mock_calibrate(real_mdata, mdata, is_import, peak_start_minutes, peak_end_minutes):
        calibrate_calls.append({"real_mdata": real_mdata, "is_import": is_import})
        return mdata

    future.futurerate_calibrate = mock_calibrate

    real_import_rates = {0: 15.0}
    real_export_rates = {0: 5.0}
    future.futurerate_analysis_new(NORDPOOL_URL, real_import_rates, real_export_rates)

    import_call = next((c for c in calibrate_calls if c["is_import"]), None)
    if import_call is None:
        print("ERROR: futurerate_calibrate not called for import")
        return True
    if import_call["real_mdata"] != real_import_rates:
        print("ERROR: expected real import rates for manual adjust_import=True, got {}".format(import_call["real_mdata"]))
        return True

    export_call = next((c for c in calibrate_calls if not c["is_import"]), None)
    if export_call is None:
        print("ERROR: futurerate_calibrate not called for export")
        return True
    if export_call["real_mdata"] != {}:
        print("ERROR: expected empty dict for manual adjust_export=False, got {}".format(export_call["real_mdata"]))
        return True

    return False


def _test_futurerate_adjust_auto_logs_detected_flags(my_predbat):
    """futurerate_adjust_auto=True logs which of import/export was detected as Agile."""
    base = MockFutureRateBase()
    base.args["futurerate_adjust_auto"] = True
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    base.args["metric_octopus_export"] = EXPORT_ENTITY
    base.set_entity_attr(IMPORT_ENTITY, "tariff", "AGILE-24-10-01")
    base.set_entity_attr(EXPORT_ENTITY, "tariff", "FLUX-EXPORT")
    future = _make_future_rate(base)

    mock_data = _make_nordpool_data(base, hours=2)
    future.download_futurerate_data = lambda url: mock_data
    future.futurerate_calibrate = lambda real, mdata, is_import, **kw: mdata

    future.futurerate_analysis_new(NORDPOOL_URL, {}, {})

    auto_log = next((m for m in base.log_messages if "agile" in m.lower() and ("true" in m.lower() or "false" in m.lower())), None)
    if auto_log is None:
        print("ERROR: Expected a log message reporting Agile detection for futurerate_adjust_auto, got: {}".format(base.log_messages))
        return True
    return False


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

_SUBTESTS = [
    ("import_is_agile_via_tariff", _test_import_is_agile_via_tariff),
    ("import_is_agile_via_tariff_code", _test_import_is_agile_via_tariff_code),
    ("export_is_agile", _test_export_is_agile),
    ("both_are_agile", _test_both_are_agile),
    ("non_agile_tariff", _test_non_agile_tariff),
    ("no_entities_configured", _test_no_entities_configured),
    ("tariff_case_insensitive", _test_tariff_case_insensitive),
    ("futurerate_auto_no_url", _test_futurerate_auto_no_url),
    ("futurerate_auto_disabled_proceeds", _test_futurerate_auto_disabled_proceeds),
    ("futurerate_auto_no_agile_returns_empty", _test_futurerate_auto_no_agile_returns_empty),
    ("futurerate_auto_import_agile_proceeds", _test_futurerate_auto_import_agile_proceeds),
    ("futurerate_auto_export_agile_proceeds", _test_futurerate_auto_export_agile_proceeds),
    ("futurerate_adjust_auto_import_agile_calibrates_import", _test_futurerate_adjust_auto_import_agile_calibrates_import),
    ("futurerate_adjust_auto_export_agile_calibrates_export", _test_futurerate_adjust_auto_export_agile_calibrates_export),
    ("futurerate_adjust_auto_disabled_uses_manual_flags", _test_futurerate_adjust_auto_disabled_uses_manual_flags),
    ("futurerate_adjust_auto_logs_detected_flags", _test_futurerate_adjust_auto_logs_detected_flags),
]


def test_futurerate_auto(my_predbat=None):
    """Run all futurerate_auto / futurerate_adjust_auto unit tests."""
    print("**** Running futurerate_auto tests ****")
    failed = False
    for name, fn in _SUBTESTS:
        result = fn(my_predbat)
        if result:
            print("  FAIL: {}".format(name))
            failed = True
        else:
            print("  PASS: {}".format(name))
    return failed
