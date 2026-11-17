# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime

from futurerate import FutureRate
from octopus import OctopusAPI


def _capture_log(my_predbat, func):
    """
    Run func() while capturing everything my_predbat.log() is given, returning (result, messages)
    """
    log_messages = []
    orig_log = my_predbat.log
    my_predbat.log = lambda msg, *args, **kwargs: log_messages.append(str(msg))
    try:
        result = func()
    finally:
        my_predbat.log = orig_log
    return result, log_messages


def test_fetch_spot_rates(my_predbat):
    """
    Test the generic spot rate path (metric_spot_import / metric_spot_export):

    - fetch_spot_rates() reads only raw_today / raw_tomorrow
    - start/end/value and from/till/price are scaled by 100 (currency/kWh into pence/cents)
    - from/to/rate and valid_from/valid_to/value_inc_vat are already pence/cents and not scaled
    - the same sensor gives the same rates through fetch_octopus_rates() (migration is a pure rename)
    - rates / prices attributes and Octopus event sensors are NOT read
    - Octopus Intelligent adjusted slots (io_adjusted) are never touched
    - a metric_spot_* sensor is never detected as Agile and gives no AgilePredict region
    - the deprecation warning for raw_today/raw_tomorrow under metric_octopus_* is one-off and only
      given when the rates really came from those attributes
    - the one-off warning helper logs each key once
    """
    print("**** Running fetch_spot_rates tests ****")
    failed = False

    old_forecast_days = my_predbat.forecast_days
    old_midnight_utc = my_predbat.midnight_utc
    old_io_adjusted = my_predbat.io_adjusted
    old_args = my_predbat.args
    old_warned = getattr(my_predbat, "spot_rates_warned", set())

    my_predbat.forecast_days = 2
    my_predbat.midnight_utc = datetime.strptime("2025-01-01T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    my_predbat.io_adjusted = {}
    my_predbat.args = dict(old_args)
    my_predbat.spot_rates_warned = set()
    my_predbat.ha_interface.dummy_items.clear()

    try:
        # Test 1: start / end / value in currency per kWh (the Nord Pool package / Czech style) is scaled by 100
        print("*** Test 1: start/end/value is scaled by 100")
        my_predbat.ha_interface.dummy_items["sensor.spot_start_end"] = {
            "state": "0.155",
            "raw_today": [
                {"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-01T00:15:00+00:00", "value": 0.155},
                {"start": "2025-01-01T00:15:00+00:00", "end": "2025-01-01T00:30:00+00:00", "value": 0.2},
            ],
        }
        rate_data = my_predbat.fetch_spot_rates("sensor.spot_start_end")
        if abs(rate_data.get(0, -1) - 15.5) > 0.001 or abs(rate_data.get(14, -1) - 15.5) > 0.001:
            print("ERROR: Test 1 expected 15.5 for minutes 0-14, got {} / {}".format(rate_data.get(0), rate_data.get(14)))
            failed = True
        elif abs(rate_data.get(15, -1) - 20.0) > 0.001 or abs(rate_data.get(29, -1) - 20.0) > 0.001:
            print("ERROR: Test 1 expected 20.0 for minutes 15-29, got {} / {}".format(rate_data.get(15), rate_data.get(29)))
            failed = True
        elif 30 in rate_data:
            print("ERROR: Test 1 expected the rate to stop at minute 30 (end is exclusive)")
            failed = True
        else:
            print("Test 1 passed")

        # Test 2: from / till / price (Frank Energie and Nord Pool style) is scaled by 100
        print("*** Test 2: from/till/price is scaled by 100")
        my_predbat.ha_interface.dummy_items["sensor.spot_price"] = {
            "state": "0.155",
            "raw_today": [{"price": 0.155, "from": "2025-01-01T00:00:00+00:00", "till": "2025-01-01T01:00:00+00:00"}],
        }
        rate_data = my_predbat.fetch_spot_rates("sensor.spot_price")
        if abs(rate_data.get(0, -1) - 15.5) > 0.001 or abs(rate_data.get(59, -1) - 15.5) > 0.001 or 60 in rate_data:
            print("ERROR: Test 2 expected 15.5 for minutes 0-59 only, got {} / {} / {}".format(rate_data.get(0), rate_data.get(59), rate_data.get(60)))
            failed = True
        else:
            print("Test 2 passed")

        # Test 3: rate/from/to and value_inc_vat/valid_from/valid_to are already pence/cents - not scaled
        print("*** Test 3: rate/from/to and value_inc_vat/valid_from/valid_to are not scaled")
        my_predbat.ha_interface.dummy_items["sensor.spot_rate"] = {
            "state": "15.5",
            "raw_today": [{"rate": 15.5, "from": "2025-01-01T00:00:00+00:00", "to": "2025-01-01T00:30:00+00:00"}],
        }
        my_predbat.ha_interface.dummy_items["sensor.spot_vat"] = {
            "state": "16.5",
            "raw_today": [{"value_inc_vat": 16.5, "valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:30:00+00:00"}],
        }
        rate_data = my_predbat.fetch_spot_rates("sensor.spot_rate")
        rate_data_vat = my_predbat.fetch_spot_rates("sensor.spot_vat")
        if rate_data.get(0) != 15.5 or rate_data.get(29) != 15.5 or 30 in rate_data:
            print("ERROR: Test 3 rate/from/to expected 15.5 for minutes 0-29, got {}".format(rate_data))
            failed = True
        elif rate_data_vat.get(0) != 16.5 or rate_data_vat.get(29) != 16.5:
            print("ERROR: Test 3 value_inc_vat expected 16.5 for minutes 0-29, got {}".format(rate_data_vat))
            failed = True
        else:
            print("Test 3 passed")

        # Test 4: raw_tomorrow is added after raw_today
        print("*** Test 4: raw_tomorrow is appended")
        my_predbat.ha_interface.dummy_items["sensor.spot_tomorrow"] = {
            "state": "0.1",
            "raw_today": [{"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-01T00:30:00+00:00", "value": 0.1}],
            "raw_tomorrow": [{"start": "2025-01-02T00:00:00+00:00", "end": "2025-01-02T00:30:00+00:00", "value": 0.2}],
        }
        rate_data = my_predbat.fetch_spot_rates("sensor.spot_tomorrow")
        if abs(rate_data.get(0, -1) - 10.0) > 0.001 or abs(rate_data.get(24 * 60, -1) - 20.0) > 0.001:
            print("ERROR: Test 4 expected 10.0 today and 20.0 tomorrow, got {} / {}".format(rate_data.get(0), rate_data.get(24 * 60)))
            failed = True
        else:
            print("Test 4 passed")

        # Test 5: raw_tomorrow not yet published is not a warning, a missing raw_today is
        print("*** Test 5: missing raw_tomorrow is silent, missing raw_today warns")
        _, messages = _capture_log(my_predbat, lambda: my_predbat.fetch_spot_rates("sensor.spot_start_end"))
        if any("raw_tomorrow" in msg for msg in messages):
            print("ERROR: Test 5 a missing raw_tomorrow should not be logged, got {}".format(messages))
            failed = True
        my_predbat.ha_interface.dummy_items["sensor.spot_no_today"] = {"state": "1", "raw_tomorrow": [{"start": "2025-01-02T00:00:00+00:00", "end": "2025-01-02T00:30:00+00:00", "value": 0.2}]}
        _, messages = _capture_log(my_predbat, lambda: my_predbat.fetch_spot_rates("sensor.spot_no_today"))
        if not any("raw_today" in msg for msg in messages):
            print("ERROR: Test 5 a missing raw_today should be logged, got {}".format(messages))
            failed = True
        else:
            print("Test 5 passed")

        # Test 6: Only raw_today / raw_tomorrow are read - not rates, prices or Octopus event sensors
        print("*** Test 6: rates / prices attributes and Octopus event sensors are not read")
        my_predbat.ha_interface.dummy_items["sensor.spot_other_attrs"] = {
            "state": "9",
            "rates": [{"value_inc_vat": 9.0, "valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:30:00+00:00"}],
            "prices": [{"price": 0.09, "from": "2025-01-01T00:00:00+00:00", "till": "2025-01-01T00:30:00+00:00"}],
        }
        rate_data = my_predbat.fetch_spot_rates("sensor.spot_other_attrs")
        my_predbat.ha_interface.dummy_items["event.octopus_energy_electricity_abc123_current_day_rates"] = {
            "state": "on",
            "rates": [{"value_inc_vat": 9.0, "valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:30:00+00:00"}],
        }
        rate_data_event = my_predbat.fetch_spot_rates("sensor.octopus_energy_electricity_abc123_current_rate")
        if rate_data != {} or rate_data_event != {}:
            print("ERROR: Test 6 expected {{}} for a sensor with no raw_today, got {} / {}".format(rate_data, rate_data_event))
            failed = True
        else:
            print("Test 6 passed")

        # Test 7: None / empty / missing / no attributes
        print("*** Test 7: None, empty, missing and attribute-less sensors return an empty dict")
        my_predbat.ha_interface.dummy_items["sensor.spot_no_attrs"] = {"state": "15.5"}
        results = [my_predbat.fetch_spot_rates(None), my_predbat.fetch_spot_rates(""), my_predbat.fetch_spot_rates("sensor.spot_nonexistent"), my_predbat.fetch_spot_rates("sensor.spot_no_attrs")]
        if any(result != {} for result in results):
            print("ERROR: Test 7 expected empty dicts, got {}".format(results))
            failed = True
        else:
            print("Test 7 passed")

        # Test 8: The same sensor gives the same rates through fetch_octopus_rates() - moving to the new name is a pure rename
        print("*** Test 8: fetch_spot_rates() matches fetch_octopus_rates() for the same sensor")
        mismatch = False
        for entity_id in ["sensor.spot_start_end", "sensor.spot_price", "sensor.spot_rate", "sensor.spot_vat", "sensor.spot_tomorrow"]:
            if my_predbat.fetch_spot_rates(entity_id) != my_predbat.fetch_octopus_rates(entity_id):
                print("ERROR: Test 8 {} gives different rates through the spot and Octopus paths".format(entity_id))
                mismatch = True
        if mismatch:
            failed = True
        else:
            print("Test 8 passed")

        # Test 9: io_adjusted (Octopus Intelligent) is never touched by a spot fetch
        print("*** Test 9: io_adjusted is not modified")
        my_predbat.io_adjusted = {123: True}
        my_predbat.fetch_spot_rates("sensor.spot_start_end")
        if my_predbat.io_adjusted != {123: True}:
            print("ERROR: Test 9 io_adjusted was changed to {}".format(my_predbat.io_adjusted))
            failed = True
        else:
            print("Test 9 passed")
        my_predbat.io_adjusted = {}

        # Test 10: A metric_spot_* sensor is never Agile, and gives no AgilePredict region
        print("*** Test 10: metric_spot_* is invisible to Agile detection and the AgilePredict region lookup")
        # Even a sensor that carries an Agile tariff attribute must not be seen - only metric_octopus_* is looked at
        my_predbat.ha_interface.dummy_items["sensor.spot_agile_like"] = {
            "state": "15.5",
            "tariff_code": "E-1R-AGILE-24-10-01-A",
            "raw_today": [{"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-01T00:30:00+00:00", "value": 0.155}],
        }
        for key in ["metric_octopus_import", "metric_octopus_export", "futurerate_agilepredict_region"]:
            my_predbat.args.pop(key, None)
        my_predbat.args["metric_spot_import"] = "sensor.spot_agile_like"
        my_predbat.args["metric_spot_export"] = "sensor.spot_agile_like"
        futurerate = FutureRate(my_predbat)
        import_agile, export_agile = futurerate.import_export_is_agile()
        region = futurerate.agilepredict_region()
        if import_agile or export_agile:
            print("ERROR: Test 10 metric_spot_* was detected as Agile (import {} export {})".format(import_agile, export_agile))
            failed = True
        elif region:
            print("ERROR: Test 10 metric_spot_* gave an AgilePredict region {}".format(region))
            failed = True
        else:
            print("Test 10 passed")
        my_predbat.args.pop("metric_spot_import", None)
        my_predbat.args.pop("metric_spot_export", None)

        # Test 11: The one-off warning helper logs each key once
        print("*** Test 11: spot_rates_warn_once logs each key once")
        my_predbat.spot_rates_warned = set()
        _, messages = _capture_log(
            my_predbat,
            lambda: [my_predbat.spot_rates_warn_once("key_a", "message A"), my_predbat.spot_rates_warn_once("key_a", "message A"), my_predbat.spot_rates_warn_once("key_b", "message B")],
        )
        if messages != ["message A", "message B"]:
            print("ERROR: Test 11 expected each key logged once, got {}".format(messages))
            failed = True
        else:
            print("Test 11 passed")

        # Test 12: Deprecation warning when metric_octopus_* is fed from raw_today / raw_tomorrow - one-off
        print("*** Test 12: raw_today under metric_octopus_* gives a one-off deprecation warning")
        my_predbat.spot_rates_warned = set()
        _, messages = _capture_log(
            my_predbat,
            lambda: [
                my_predbat.fetch_octopus_rates("sensor.spot_start_end", spot_arg="metric_spot_import"),
                my_predbat.fetch_octopus_rates("sensor.spot_start_end", spot_arg="metric_spot_import"),
            ],
        )
        warnings = [msg for msg in messages if "metric_spot_import" in msg]
        if len(warnings) != 1:
            print("ERROR: Test 12 expected exactly one deprecation warning naming metric_spot_import, got {}".format(messages))
            failed = True
        else:
            print("Test 12 passed")

        # The import and export warnings are separate (one each)
        _, messages = _capture_log(my_predbat, lambda: my_predbat.fetch_octopus_rates("sensor.spot_start_end", spot_arg="metric_spot_export"))
        if len([msg for msg in messages if "metric_spot_export" in msg]) != 1:
            print("ERROR: Test 12 expected a separate warning for the export, got {}".format(messages))
            failed = True

        # Test 13: No warning when the rates did not come from raw_today / raw_tomorrow, when spot_arg is not given, or nothing loaded
        print("*** Test 13: no deprecation warning for genuine Octopus sensors, gas, or failed loads")
        my_predbat.spot_rates_warned = set()
        my_predbat.ha_interface.dummy_items["sensor.octopus_real"] = {
            "state": "15.5",
            "rates": [{"value_inc_vat": 15.5, "valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:30:00+00:00"}],
        }
        my_predbat.ha_interface.dummy_items["sensor.frank_like"] = {
            "state": "0.1",
            "prices": [{"price": 0.1, "from": "2025-01-01T00:00:00+00:00", "till": "2025-01-01T00:30:00+00:00"}],
        }
        _, messages_real = _capture_log(my_predbat, lambda: my_predbat.fetch_octopus_rates("sensor.octopus_real", spot_arg="metric_spot_import"))
        _, messages_prices = _capture_log(my_predbat, lambda: my_predbat.fetch_octopus_rates("sensor.frank_like", spot_arg="metric_spot_import"))
        _, messages_no_arg = _capture_log(my_predbat, lambda: my_predbat.fetch_octopus_rates("sensor.spot_start_end"))
        _, messages_missing = _capture_log(my_predbat, lambda: my_predbat.fetch_octopus_rates("sensor.spot_nonexistent", spot_arg="metric_spot_import"))
        if any("metric_spot" in msg for msg in messages_real + messages_prices + messages_no_arg + messages_missing):
            print("ERROR: Test 13 unexpected deprecation warning: {}".format(messages_real + messages_prices + messages_no_arg + messages_missing))
            failed = True
        else:
            print("Test 13 passed")

    finally:
        my_predbat.forecast_days = old_forecast_days
        my_predbat.midnight_utc = old_midnight_utc
        my_predbat.io_adjusted = old_io_adjusted
        my_predbat.args = old_args
        my_predbat.spot_rates_warned = old_warned
        my_predbat.ha_interface.dummy_items.clear()

    if not failed:
        print("**** All fetch_spot_rates tests PASSED ****")
    else:
        print("**** Some fetch_spot_rates tests FAILED ****")

    return failed


_COMPETING_ARGS = [
    "metric_spot_import",
    "metric_spot_export",
    "metric_energidataservice_import",
    "metric_energidataservice_export",
    "metric_stromligning_import_today",
    "metric_stromligning_import_tomorrow",
    "metric_stromligning_export_today",
    "metric_stromligning_export_tomorrow",
]
_AUTO_ARGS = ["metric_octopus_import", "metric_octopus_export", "metric_standing_charge"]
_NOT_SET = object()


def test_octopus_auto_rates(my_predbat):
    """
    Octopus automatic_config() must not out-rank a rate source the user chose in apps.yaml.

    metric_octopus_xxx ranks above metric_spot_xxx, metric_energidataservice_xxx and metric_stromligning_xxx in
    fetch_sensor_data(), so an auto-discovered metric_octopus_xxx would silently win over them. When the user's
    apps.yaml sets one of the lower ranked sources for a direction (import or export), that direction is left alone
    (and for import metric_standing_charge too) and a note is logged once. A direction with no such entry is
    auto-configured exactly as before, including over the user's own metric_octopus_xxx.
    """
    print("**** Running octopus_auto_rates tests ****")
    failed = False

    old_args = dict(my_predbat.args)
    old_raw = getattr(my_predbat, "args_from_apps_yaml", _NOT_SET)
    old_warned = getattr(my_predbat, "apps_yaml_override_warned", _NOT_SET)

    def run_case(raw_apps_yaml, tariffs=("import", "export"), runs=2):
        """Reset the args, run automatic_config() `runs` times with this apps.yaml, return (args that were set, Info logs)"""
        for key in _AUTO_ARGS + _COMPETING_ARGS:
            my_predbat.args.pop(key, None)
        my_predbat.args.update(raw_apps_yaml)
        my_predbat.args_from_apps_yaml = dict(raw_apps_yaml)
        my_predbat.apps_yaml_override_warned = set()
        api = OctopusAPI(my_predbat, key="", account_id="", automatic=True)
        api.tariffs = {}
        logs = []
        api.log = lambda msg, *args, **kwargs: logs.append(str(msg))
        for _ in range(runs):
            api.automatic_config({tariff: {} for tariff in tariffs})
        set_args = {key for key in _AUTO_ARGS if key in my_predbat.args and key not in raw_apps_yaml}
        return set_args, [msg for msg in logs if msg.startswith("Info: apps.yaml sets")]

    try:
        print("*** Test 1: nothing else configured - Octopus rates are auto-configured as before")
        set_args, infos = run_case({})
        if set_args != set(_AUTO_ARGS) or infos:
            print("ERROR: Test 1 expected all of {} set and no notes, got {} / {}".format(_AUTO_ARGS, sorted(set_args), infos))
            failed = True
        else:
            print("Test 1 passed")

        print("*** Test 2: user metric_spot_import - import (and the standing charge) are kept, export is still auto-configured, one note")
        set_args, infos = run_case({"metric_spot_import": "sensor.my_spot_import"})
        if set_args != {"metric_octopus_export"}:
            print("ERROR: Test 2 expected only metric_octopus_export auto-configured, got {}".format(sorted(set_args)))
            failed = True
        elif "metric_octopus_import" in my_predbat.args or my_predbat.args.get("metric_spot_import") != "sensor.my_spot_import":
            print("ERROR: Test 2 the user's import setting must be left as written and metric_octopus_import left unset")
            failed = True
        elif len(infos) != 1 or "metric_spot_import" not in infos[0] or "metric_octopus_import" not in infos[0]:
            print("ERROR: Test 2 expected exactly one note naming both settings across two runs, got {}".format(infos))
            failed = True
        else:
            print("Test 2 passed")

        print("*** Test 3: Strømligning export only - export is kept, import is auto-configured")
        set_args, infos = run_case({"metric_stromligning_export_tomorrow": "sensor.s_export_tomorrow"})
        if set_args != {"metric_octopus_import", "metric_standing_charge"}:
            print("ERROR: Test 3 expected import and standing charge auto-configured, got {}".format(sorted(set_args)))
            failed = True
        elif len(infos) != 1 or "metric_octopus_export" not in infos[0]:
            print("ERROR: Test 3 expected one note about metric_octopus_export, got {}".format(infos))
            failed = True
        else:
            print("Test 3 passed")

        print("*** Test 4: Energi Data Service import and export - nothing for rates is auto-configured")
        set_args, infos = run_case({"metric_energidataservice_import": "sensor.eds", "metric_energidataservice_export": "sensor.eds_export"})
        if set_args:
            print("ERROR: Test 4 expected no Octopus rate args auto-configured, got {}".format(sorted(set_args)))
            failed = True
        elif len(infos) != 2:
            print("ERROR: Test 4 expected one note per direction, got {}".format(infos))
            failed = True
        else:
            print("Test 4 passed")

        print("*** Test 5: the user's own metric_octopus_import (the default apps.yaml regular expression) does not stop auto-configuration")
        set_args, infos = run_case({"metric_octopus_import": "re:(sensor.octopus_energy_electricity_[0-9a-z]+_[0-9a-z]+_current_rate)"})
        if "metric_octopus_import" in set_args or my_predbat.args.get("metric_octopus_import", "").startswith("re:"):
            print("ERROR: Test 5 auto-discovery should still replace the user's metric_octopus_import, got {}".format(my_predbat.args.get("metric_octopus_import")))
            failed = True
        elif infos:
            print("ERROR: Test 5 expected no note, got {}".format(infos))
            failed = True
        else:
            print("Test 5 passed")

        print("*** Test 6: no snapshot of apps.yaml available - behaves as if nothing is configured")
        for key in _AUTO_ARGS + _COMPETING_ARGS:
            my_predbat.args.pop(key, None)
        if hasattr(my_predbat, "args_from_apps_yaml"):
            del my_predbat.args_from_apps_yaml
        api = OctopusAPI(my_predbat, key="", account_id="", automatic=True)
        api.tariffs = {}
        api.automatic_config({"import": {}, "export": {}})
        if not all(key in my_predbat.args for key in _AUTO_ARGS):
            print("ERROR: Test 6 expected the Octopus rates auto-configured, got {}".format({key: my_predbat.args.get(key) for key in _AUTO_ARGS}))
            failed = True
        else:
            print("Test 6 passed")

    finally:
        my_predbat.args.clear()
        my_predbat.args.update(old_args)
        if old_raw is _NOT_SET:
            if hasattr(my_predbat, "args_from_apps_yaml"):
                del my_predbat.args_from_apps_yaml
        else:
            my_predbat.args_from_apps_yaml = old_raw
        if old_warned is _NOT_SET:
            if hasattr(my_predbat, "apps_yaml_override_warned"):
                del my_predbat.apps_yaml_override_warned
        else:
            my_predbat.apps_yaml_override_warned = old_warned

    if not failed:
        print("**** All octopus_auto_rates tests PASSED ****")
    else:
        print("**** Some octopus_auto_rates tests FAILED ****")

    return failed
