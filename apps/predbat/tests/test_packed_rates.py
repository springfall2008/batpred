# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for PackedRates, the read-only array-backed form fetch.py holds its finished per-minute rate series in."""

import copy
import io
import sys

from futurerate import FutureRate
from utils import PackedRates
from userinterface import dump_debug_yaml
from agent_tools import measure_state_value, summarise_state_value


def _series(start=0, minutes=600):
    """A half hourly rate held for each consecutive minute, as the rate builders produce."""
    return {start + minute: round(10.0 + 0.37 * ((minute // 30) % 17) - 0.125 * ((minute // 30) % 5), 4) for minute in range(minutes)}


def _reference_min_forward(my_predbat, rates):
    """rate_min_forward_calc() as it was when it built a dict, to compare the packed result against."""
    rate_array = []
    result = {}
    rate = my_predbat.rate_min
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60):
        if minute in rates:
            rate = rates[minute]
        rate_array.append(rate)
    running_min = rate_array[-1] if rate_array else my_predbat.rate_min
    for minute in range(len(rate_array) - 1, my_predbat.minutes_now - 1, -1):
        running_min = min(running_min, rate_array[minute])
        if minute < my_predbat.forecast_minutes + 24 * 60 + my_predbat.minutes_now:
            result[minute] = running_min
    return result


def _reference_max_forward(my_predbat, rates):
    """rate_export_max_forward_calc() as it was when it built a dict."""
    rate_array = []
    result = {}
    rate = 0.0
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 48 * 60):
        if minute in rates:
            rate = rates[minute]
        rate_array.append(rate)
    running_max = rate_array[-1] if rate_array else 0.0
    for minute in range(len(rate_array) - 1, my_predbat.minutes_now - 1, -1):
        running_max = max(running_max, rate_array[minute])
        if minute < my_predbat.forecast_minutes + 24 * 60 + my_predbat.minutes_now:
            result[minute] = running_max
    return result


def _same_as_dict(packed, expected, label):
    """True when packed reads exactly as the dict expected does, key order included; prints what differs."""
    checks = [
        ("type", isinstance(packed, PackedRates)),
        ("==", packed == expected and expected == packed and not (packed != expected)),
        ("len", len(packed) == len(expected)),
        ("order", list(packed) == list(expected) and list(packed.keys()) == list(expected.keys())),
        ("items", list(packed.items()) == list(expected.items())),
        ("values", list(packed.values()) == list(expected.values())),
        ("to_dict", type(packed.to_dict()) is dict and list(packed.to_dict().items()) == list(expected.items())),
        ("dict()", list(dict(packed).items()) == list(expected.items())),
        ("unpack", list(PackedRates.unpack(packed).items()) == list(expected.items())),
        ("[]", all(packed[minute] == rate for minute, rate in expected.items())),
        ("get", all(packed.get(minute) == rate for minute, rate in expected.items())),
        ("in", all(minute in packed for minute in expected)),
        ("bool", bool(packed) == bool(expected)),
        ("repr", repr(packed) == repr(expected)),
    ]
    bad = [name for name, ok in checks if not ok]
    if bad:
        print("ERROR: {}: PackedRates differs from the dict it holds in {}".format(label, bad))
    return not bad


def test_packed_rates(my_predbat):
    """PackedRates reads exactly as the dict it holds, refuses what it cannot hold, and is dumped and described as that dict."""
    failed = False
    print("**** Running PackedRates tests ****")

    # --- Test 1: a consecutive float series packs and reads as the same dict ---
    for start in (0, 7, -120):
        series = _series(start)
        packed = PackedRates.pack(series)
        failed |= not _same_as_dict(packed, series, "test 1 start {}".format(start))

    # --- Test 2: every key type behaves as it does on the dict: missing, wrong type, or equal to an int minute ---
    as_dict = {10: 1.5, 11: 2.5, 12: 3.5}
    packed = PackedRates.pack(as_dict)
    keys = [9, 10, 12, 13, -1, 10.0, 11.0, 10.5, -0.0, True, False, "10", None, float("nan"), float("inf"), (10,), b"10"]
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        keys += [numpy.int64(11), numpy.float64(12.0), numpy.float64(12.5)]
    for key in keys:
        if packed.get(key, "missing") != as_dict.get(key, "missing") or (key in packed) != (key in as_dict):
            print("ERROR: test 2: get/in for {!r} differ from the dict's: {!r}/{} against {!r}/{}".format(key, packed.get(key, "missing"), key in packed, as_dict.get(key, "missing"), key in as_dict))
            failed = True
        try:
            got = packed[key]
        except KeyError:
            got = KeyError
        try:
            want = as_dict[key]
        except (KeyError, TypeError):
            want = KeyError
        if got != want:
            print("ERROR: test 2: [{!r}] gives {!r}, the dict gives {!r}".format(key, got, want))
            failed = True

    # --- Test 3: read-only - no item assignment, no deletion ---
    for action in ("set", "del"):
        try:
            if action == "set":
                packed[10] = 9.0
            else:
                del packed[10]
            print("ERROR: test 3: PackedRates should refuse {}".format(action))
            failed = True
        except TypeError:
            pass
    if packed[10] != 1.5:
        print("ERROR: test 3: a refused write changed the series")
        failed = True

    # --- Test 4: copies are independent and still packed ---
    series = _series(30, 90)
    packed = PackedRates.pack(series)
    for name, copied in (("deepcopy", copy.deepcopy(packed)), ("copy", copy.copy(packed))):
        if not isinstance(copied, PackedRates) or copied != series or list(copied) != list(series):
            print("ERROR: test 4: {} of PackedRates should be equal PackedRates".format(name))
            failed = True
    deep = copy.deepcopy(packed)
    deep.rates[0] = -99.0
    if packed[30] == -99.0:
        print("ERROR: test 4: a deepcopy shares its values with the original")
        failed = True

    # --- Test 5: anything that is not a run of float minutes stays as it is ---
    for name, data in (
        ("gap", {0: 1.0, 1: 2.0, 3: 3.0}),
        ("descending", {2: 1.0, 1: 2.0, 0: 3.0}),
        ("int value", {0: 1.0, 1: 2}),
        ("bool value", {0: 1.0, 1: True}),
        ("str key", {"0": 1.0}),
        ("empty", {}),
        ("list", [1.0, 2.0]),
        ("none", None),
    ):
        if PackedRates.pack(data) is not data:
            print("ERROR: test 5: {} should be returned unchanged".format(name))
            failed = True

    # --- Test 6: pack_copy is always independent of the dict it copies ---
    source = _series(0, 60)
    snapshot = PackedRates.pack_copy(source)
    source[0] = 99.0
    if not isinstance(snapshot, PackedRates) or snapshot[0] == 99.0:
        print("ERROR: test 6: pack_copy should give a packed snapshot the source's later changes do not reach")
        failed = True
    gappy = {0: 1.0, 2: 2.0}
    snapshot = PackedRates.pack_copy(gappy)
    gappy[0] = 99.0
    if snapshot is gappy or type(snapshot) is not dict or snapshot != {0: 1.0, 2: 2.0}:
        print("ERROR: test 6: pack_copy of a dict it cannot pack should be a separate dict copy")
        failed = True

    # --- Test 7: a descending series keeps the order it was built in ---
    built = {}
    values = []
    for minute in range(119, 59, -1):
        built[minute] = float(minute) / 4
        values.append(float(minute) / 4)
    packed = PackedRates.from_descending(119, values)
    failed |= not _same_as_dict(packed, built, "test 7 descending")
    if PackedRates.from_descending(5, []) != {} or type(PackedRates.from_descending(5, [])) is not dict:
        print("ERROR: test 7: an empty descending series should be an empty dict")
        failed = True
    mixed = PackedRates.from_descending(3, [1.0, 2, 3.0])
    if type(mixed) is not dict or list(mixed.items()) != [(3, 1.0), (2, 2), (1, 3.0)]:
        print("ERROR: test 7: a descending series with a non-float should be the dict it would have been built as, got {!r}".format(mixed))
        failed = True

    # --- Test 8: the forward-looking series match the dicts they used to be ---
    saved = (my_predbat.minutes_now, my_predbat.forecast_minutes, my_predbat.rate_min)
    try:
        my_predbat.minutes_now = 600
        my_predbat.forecast_minutes = 24 * 60
        my_predbat.rate_min = 5.0
        rates = _series(0, 3 * 24 * 60)
        result = my_predbat.rate_min_forward_calc(rates)
        failed |= not _same_as_dict(result, _reference_min_forward(my_predbat, rates), "test 8 rate_min_forward_calc")
        result = my_predbat.rate_export_max_forward_calc(rates)
        failed |= not _same_as_dict(result, _reference_max_forward(my_predbat, rates), "test 8 rate_export_max_forward_calc")
        # An int rate (a user's whole-pence rate) keeps the series a dict, with the int intact
        rates[700] = 3
        result = my_predbat.rate_min_forward_calc(rates)
        expected = _reference_min_forward(my_predbat, rates)
        if type(result) is not dict or list(result.items()) != list(expected.items()) or not any(type(value) is int for value in result.values()):
            print("ERROR: test 8: rate_min_forward_calc with an int rate should give the same dict as before")
            failed = True
    finally:
        my_predbat.minutes_now, my_predbat.forecast_minutes, my_predbat.rate_min = saved

    # --- Test 9: a debug dump writes a packed series exactly as the dict ---
    for series in (_series(0, 50), dict(PackedRates.from_descending(49, [float(minute) for minute in range(49, -1, -1)]))):
        as_dict = io.StringIO()
        dump_debug_yaml({"rates": series}, as_dict)
        as_packed = io.StringIO()
        packed = PackedRates.pack(series) if list(series) == sorted(series) else PackedRates.from_descending(49, list(series.values()))
        dump_debug_yaml({"rates": packed}, as_packed)
        if not isinstance(packed, PackedRates) or as_packed.getvalue() != as_dict.getvalue():
            print("ERROR: test 9: the debug dump of a packed series differs from the dict's:\n{}\nagainst\n{}".format(as_packed.getvalue()[:300], as_dict.getvalue()[:300]))
            failed = True
        # A nested series is written by the direct writer (debug_value_lines) when the rest of its key is plain data,
        # and by yaml.dump when anything else in the key is not (a set here), where only DebugYamlDumper's PackedRates
        # representer keeps it a plain map rather than a !!python/object tag
        for path, wrap in (("direct", lambda value: {"nested": value}), ("yaml.dump", lambda value: {"nested": value, "other": {1, 2}})):
            nested_as_dict = io.StringIO()
            dump_debug_yaml({"rates": wrap(series)}, nested_as_dict)
            nested_as_packed = io.StringIO()
            dump_debug_yaml({"rates": wrap(packed)}, nested_as_packed)
            if nested_as_packed.getvalue() != nested_as_dict.getvalue() or "!!python" in nested_as_packed.getvalue():
                print("ERROR: test 9: the {} debug dump of a nested packed series differs from the dict's:\n{}\nagainst\n{}".format(path, nested_as_packed.getvalue()[:300], nested_as_dict.getvalue()[:300]))
                failed = True

    # --- Test 10: the MCP state description of the unpacked series matches the dict's ---
    series = _series(0, 2000)
    packed = PackedRates.pack(series)
    if summarise_state_value(PackedRates.unpack(packed)) != summarise_state_value(series) or measure_state_value(PackedRates.unpack(packed), 10**7) != measure_state_value(series, 10**7):
        print("ERROR: test 10: MCP describes an unpacked series differently from the dict")
        failed = True

    # --- Test 11: the real fetch_sensor_data() leaves its finished series packed ---
    failed |= _check_fetch_packs_series(my_predbat)

    if not failed:
        print("PackedRates tests passed")
    return failed


# The series fetch_sensor_data() holds packed, and the ones it must leave as dicts because later code changes them in place
FETCH_PACKED_SERIES = (
    "rate_import_base",
    "rate_import_no_io",
    "rate_min_forward",
    "rate_export_base",
    "rate_export_max_forward",
    "rate_gas",
    "future_energy_rates_import",
    "future_energy_rates_export",
)
FETCH_DICT_SERIES = ("rate_import", "rate_export")
# Every apps.yaml rate source fetch_sensor_data() tries ahead of the basic rates_* lists this test sets
FETCH_RATE_SOURCES = (
    "rates_import_octopus_url",
    "metric_octopus_import",
    "metric_energidataservice_import",
    "metric_stromligning_import_today",
    "metric_stromligning_import_tomorrow",
    "rates_export_octopus_url",
    "metric_octopus_export",
    "metric_energidataservice_export",
    "metric_stromligning_export_today",
    "metric_stromligning_export_tomorrow",
    "metric_octopus_gas",
    "futurerate_url",
)


class _StopFetch(Exception):
    """Raised by the test's hook to stop fetch_sensor_data() once every rate series is built."""


def _fetch_series(my_predbat, rates_import):
    """
    Run the real fetch_sensor_data() with basic import, export and gas rates, stop it at set_rate_thresholds() (which
    follows the last of the series being built) and return {name: series}. Restores the instance and its args afterwards.
    """
    saved_state = dict(my_predbat.__dict__)
    saved_args = copy.deepcopy(my_predbat.args)
    saved_analysis = FutureRate.futurerate_analysis
    seen = {}

    def stop(*args, **kwargs):
        """Record every series as fetch_sensor_data() left it, then stop the fetch."""
        for name in FETCH_PACKED_SERIES + FETCH_DICT_SERIES:
            seen[name] = getattr(my_predbat, name, None)
        raise _StopFetch()

    try:
        for key in FETCH_RATE_SOURCES:
            my_predbat.args.pop(key, None)
        my_predbat.args["rates_import"] = rates_import
        my_predbat.args["rates_export"] = [{"rate": 15.0}]
        my_predbat.args["rates_gas"] = [{"rate": 6.5}]
        my_predbat.set_rate_thresholds = stop
        # A predicted price for every minute ahead, as a configured futurerate source gives, without fetching one
        FutureRate.futurerate_analysis = lambda self, rate_import_real, rate_export_real: (
            {minute: 20.0 + (minute // 30) % 3 for minute in range(48 * 60)},
            {minute: 12.0 + (minute // 30) % 2 for minute in range(48 * 60)},
        )
        # The shared fixture's sensor history can't feed the load and energy reads ahead of the rates, and they play no part in them
        my_predbat.minute_data_load = lambda *args, **kwargs: ({}, 0)
        my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
        # Nor do free electricity sessions, and the fixture's octopus_free_url would otherwise fetch the live page
        my_predbat.download_octopus_free = lambda url: []
        try:
            my_predbat.fetch_sensor_data(save=False)
            print("ERROR: test 11: fetch_sensor_data() returned without reaching set_rate_thresholds()")
        except _StopFetch:
            pass
    finally:
        FutureRate.futurerate_analysis = saved_analysis
        for name in [name for name in my_predbat.__dict__ if name not in saved_state]:
            delattr(my_predbat, name)
        my_predbat.__dict__.update(saved_state)
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
    return seen


def _check_fetch_packs_series(my_predbat):
    """Every series fetch_sensor_data() holds packed is PackedRates after a real fetch, and a whole-pence tariff gap leaves its series a dict."""
    failed = False
    # A day split into a cheap night and a day rate, so every minute is covered by a float rate. basic_rates() builds
    # minutes 0 on and rate_replicate() fills in the day before, so this also checks that fill leaves the minutes in order
    seen = _fetch_series(my_predbat, [{"start": "00:00:00", "end": "07:00:00", "rate": 7.5}, {"start": "07:00:00", "end": "00:00:00", "rate": 28.0}])
    if not seen:
        return True
    for name in FETCH_PACKED_SERIES:
        if not isinstance(seen[name], PackedRates):
            print("ERROR: test 11: fetch_sensor_data() should leave {} packed, got {}".format(name, type(seen[name]).__name__))
            failed = True
    for name in FETCH_DICT_SERIES:
        if type(seen[name]) is not dict:
            print("ERROR: test 11: fetch_sensor_data() must leave {} a dict (later code changes it in place), got {}".format(name, type(seen[name]).__name__))
            failed = True
    if not failed and (seen["rate_import_base"].get(my_predbat.minutes_now) is None or seen["rate_gas"].get(my_predbat.minutes_now) != 6.5):
        print("ERROR: test 11: the packed series do not hold the fetched rates")
        failed = True

    # Minutes no rates_import entry covers are the int 0 basic_rates() starts from, so the import series cannot be
    # packed and stays the dict it is, with that int intact; export and gas are untouched by it and still pack
    seen = _fetch_series(my_predbat, [{"start": "07:00:00", "end": "00:00:00", "rate": 28.0}])
    if not seen:
        return True
    if type(seen["rate_import_base"]) is not dict or not any(type(rate) is int for rate in seen["rate_import_base"].values()):
        print("ERROR: test 11: an import tariff with an int-rate gap should leave rate_import_base the dict it was built as, got {}".format(type(seen["rate_import_base"]).__name__))
        failed = True
    for name in ("rate_export_base", "rate_gas"):
        if not isinstance(seen[name], PackedRates):
            print("ERROR: test 11: {} should still be packed when only the import tariff has a gap, got {}".format(name, type(seen[name]).__name__))
            failed = True
    return failed
