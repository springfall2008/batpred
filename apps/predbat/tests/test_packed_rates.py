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

    # --- Test 10: the MCP state description of the unpacked series matches the dict's ---
    series = _series(0, 2000)
    packed = PackedRates.pack(series)
    if summarise_state_value(PackedRates.unpack(packed)) != summarise_state_value(series) or measure_state_value(PackedRates.unpack(packed), 10**7) != measure_state_value(series, 10**7):
        print("ERROR: test 10: MCP describes an unpacked series differently from the dict")
        failed = True

    if not failed:
        print("PackedRates tests passed")
    return failed
