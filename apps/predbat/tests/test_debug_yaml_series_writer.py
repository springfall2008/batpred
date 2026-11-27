# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""dump_debug_yaml writes plain data directly, and the dump reads back exactly as yaml.dump's would."""

import array
import functools
import io
import itertools
import math
import os
import sys
import tempfile

import yaml

from userinterface import DebugYamlDumper, dump_debug_yaml, load_debug_yaml_file, write_debug_value
from utils import MinuteArray

# Floats whose text PyYAML has to read back as the same float: no decimal point, exponents, the extremes, signed zero
AWKWARD_FLOATS = [0.0, -0.0, 1.0, -2.5, 0.1, 1e-05, 1.5e-07, 1e16, -1e16, 1.7976931348623157e308, 5e-324, 123456.789, float("nan"), float("inf"), float("-inf")]

# Strings a double-quoted scalar has to carry exactly: quotes, escapes, YAML-looking text, line breaks YAML folds,
# characters PyYAML's reader refuses, and characters outside the BMP
AWKWARD_STRINGS = [
    "",
    "plain",
    "yes",
    "null",
    "1.5",
    "0x1f",
    "~",
    "- item",
    "key: value # not a comment",
    "  leading and trailing  ",
    'a "quoted" word',
    "back\\slash",
    "line\nbreak\r\nand\ttab",
    "bell\x07 escape\x1b del\x7f",
    "nel\x85 ls\u2028 ps\u2029",
    "bom\ufeff and the reserved\ufffe\uffff",
    "accents caf\u00e9 and \u00fc",
    "emoji \U0001f50b\u26a1",
    "<table><tr><td>html</td></tr></table>",
    "%YAML 1.1",
    "!tag",
    "&anchor *alias",
    "{not: a mapping}",
    "[not, a, list]",
]


def old_dump(debug):
    """The dump as dump_debug_yaml wrote it before the direct writer: yaml.dump for every top-level key."""
    stream = io.StringIO()
    dumper = functools.partial(DebugYamlDumper, anchor_ids=itertools.count(1))
    for key in sorted(debug):
        yaml.dump({key: debug[key]}, stream, Dumper=dumper)
    return stream.getvalue()


def new_dump(debug):
    """The dump as dump_debug_yaml writes it now."""
    stream = io.StringIO()
    dump_debug_yaml(debug, stream)
    return stream.getvalue()


def same(a, b, path="root"):
    """Return None when a and b are equal with the same types all the way down (NaN equal to NaN, signed zeros kept), else where they differ."""
    if type(a) is not type(b):
        return "{}: type {} vs {}".format(path, type(a).__name__, type(b).__name__)
    if isinstance(a, MinuteArray):
        return same(a._data, b._data, path + "._data")
    if isinstance(a, array.array):
        if a.typecode != b.typecode:
            return "{}: typecode {} vs {}".format(path, a.typecode, b.typecode)
        return same(a.tolist(), b.tolist(), path)
    if isinstance(a, dict):
        if list(a) != list(b):
            return "{}: keys or key order differ".format(path)
        for key in a:
            found = same(a[key], b[key], "{}.{}".format(path, key))
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "{}: length {} vs {}".format(path, len(a), len(b))
        for index, (x, y) in enumerate(zip(a, b)):
            found = same(x, y, "{}[{}]".format(path, index))
            if found:
                return found
        return None
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return None
    if isinstance(a, float) and a == 0.0 and math.copysign(1, a) != math.copysign(1, b):
        return "{}: signed zero {} vs {}".format(path, a, b)
    return None if a == b else "{}: {!r} vs {!r}".format(path, a, b)


def plain_debug():
    """Plain data of every shape the direct writer takes, that yaml.safe_load can read."""
    return {
        "load_minutes": {minute: minute * 0.0123 for minute in range(31680)},
        "rate_min_forward": {minute: 10.5 for minute in range(5000, 1000, -1)},  # built last minute first
        "awkward_floats": {index - 7: value for index, value in enumerate(AWKWARD_FLOATS)},
        "awkward_float_list": AWKWARD_FLOATS * 3,
        "awkward_strings": AWKWARD_STRINGS,
        "awkward_keys": {text: index for index, text in enumerate(AWKWARD_STRINGS)},
        "ints_and_floats": {-4320: 36, -4319: 36.01, 0: 0, 1: 1.5, 99999999999: 123456789012345678},
        "short_series": {0: 1.0, 30: 2.0},
        "dashboard_values": {
            "predbat.plan_html": {"state": "ok", "attributes": {"html": "<td>\n</td>", "raw": {"rows": [{"time": "00:00", "soc": 50.5, "flags": [True, False, None]}, {}]}}},
            "predbat.best_load_energy": {"state": 12.5, "attributes": {"results": {"2026-11-26T00:%02d:00+00:00" % minute: minute / 10 for minute in range(60)}, "friendly_name": "Best load"}},
            "sensor.empty": {"state": None, "attributes": {}},
        },
        "list_of_lists": [[1, 2], [], [[3.5]], ["a", {"b": [4]}]],
        "tuple_value": (1, "two", 3.0),
        "dict_in_list_with_list_first": [{"a": [1, 2], "b": 3}],
        "mixed_key_types": {1: "one", "two": 2},
        "empty_dict": {},
        "empty_list": [],
        "a_string": "text",
        "a_bool": False,
        "nothing": None,
        "on": {0: 1.0},  # a key YAML 1.1 reads as a boolean
    }


def object_debug():
    """Values only the replay loader can read: MinuteArray and array.array, at the top level and nested."""
    history = MinuteArray({minute: minute * 0.5 for minute in range(1000)}, 1000)
    return {
        "load_minutes": history,
        "short_history": MinuteArray({0: 1.5, 1: 2.5}, 3),
        "empty_history": MinuteArray({}, 0),
        "an_array": array.array("d", [1.0, 2.5, float("nan")]),
        "an_int_array": array.array("l", list(range(40))),
        "nested": {"history": MinuteArray({0: 0.25}, 2), "arrays": [array.array("d", [])]},
    }


def fallback_debug():
    """Values the direct writer must leave to yaml.dump."""
    cycle = {"a": 1}
    cycle["self"] = cycle
    debug = {
        "with_cycle": cycle,
        "bool_keys": {True: 1, False: 0},
        "float_keys": {1.5: "x"},
        "unicode_array": array.array("u", "ab"),
        "an_object": RepeatLogGateStandIn(),
        "lone_surrogate": ["fine", "lone \ud800 surrogate"],
    }
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        debug["numpy_values"] = {0: numpy.float64(1.5), 1: numpy.float64(2.5)}
    return debug


class RepeatLogGateStandIn:
    """An object of a class the direct writer does not know."""


def check(name, condition, failed):
    """Print PASS or FAIL for one check and return the updated failed flag."""
    print("{}: {}".format("PASS" if condition else "FAIL", name))
    return failed or not condition


def replay_load(text, temp_dir, name):
    """Load a dump's text with the replay loader (--debug_file), through a file as the replay reads it."""
    path = os.path.join(temp_dir, "predbat_debug_{}.yaml".format(name))
    with open(path, "w") as handle:
        handle.write(text)
    return load_debug_yaml_file(path)


def test_debug_yaml_series_writer(my_predbat=None):
    """The direct writer's dump reads back identical to yaml.dump's, through yaml.safe_load and the replay loader."""
    print("**** Running debug yaml direct writer tests ****")
    failed = False

    # Plain data: yaml.safe_load reads the new dump exactly as the old one
    plain = plain_debug()
    old_text, new_text = old_dump(plain), new_dump(plain)
    found = same(yaml.safe_load(new_text), yaml.safe_load(old_text))
    failed = check("yaml.safe_load reads plain data back as yaml.dump's dump{}".format(" - " + found if found else ""), not found, failed)
    unwritten = [key for key in plain if not write_debug_value(key, plain[key], io.StringIO())]
    failed = check("every plain key written directly{}".format(" - not: {}".format(unwritten) if unwritten else ""), not unwritten, failed)
    # Long number runs are compact; short ones and nested data keep yaml.dump's one-per-line layout
    failed = check("a long minute series is written compactly", "\nload_minutes: {0: 0.0, 1: 0.0123" in "\n" + new_text, failed)
    failed = check("a short series keeps one entry per line", "\nshort_series:\n  0: 1.0\n  30: 2.0\n" in "\n" + new_text, failed)
    failed = check("nested data is laid out in block style", "\ndashboard_values:\n  predbat.best_load_energy:\n" in "\n" + new_text or '\ndashboard_values:\n  "predbat.best_load_energy":\n' in "\n" + new_text, failed)
    # An object held twice inside a key is written out each time (yaml.dump would alias it) and loads back equal
    shared = [0.1, 0.2, 0.3]
    aliased = {"windows": {"today": shared, "tomorrow": shared, "list": [shared, shared]}}
    aliased_text = new_dump(aliased)
    failed = check("a shared object inside a key is written out each time, without anchors", "&id" not in aliased_text and write_debug_value("windows", aliased["windows"], io.StringIO()), failed)
    found = same(yaml.safe_load(aliased_text), yaml.safe_load(old_dump(aliased)))
    failed = check("a shared object inside a key loads back equal to yaml.dump's alias{}".format(" - " + found if found else ""), not found, failed)
    # yaml.dump escapes everything outside printable ASCII, so the dump never depended on a file's encoding; nor may this
    non_ascii = sorted({char for char in new_text if not " " <= char <= "~" and char != "\n"})
    failed = check("the dump is printable ASCII, as yaml.dump's is{}".format(" - not: {!r}".format(non_ascii) if non_ascii else ""), not non_ascii, failed)
    failed = check("yaml.dump's dump of the same data is printable ASCII too", all(" " <= char <= "~" or char == "\n" for char in old_text), failed)
    longest = max(len(line) for line in new_text.splitlines())
    failed = check("no line longer than 400 characters (longest {})".format(longest), longest <= 400, failed)

    with tempfile.TemporaryDirectory() as temp_dir:
        # The replay loader reads plain data the same as well
        found = same(replay_load(new_text, temp_dir, "plain_new"), replay_load(old_text, temp_dir, "plain_old"))
        failed = check("the replay loader reads plain data back as yaml.dump's dump{}".format(" - " + found if found else ""), not found, failed)

        # MinuteArray and array.array: written directly, rebuilt identically by the replay loader
        objects = object_debug()
        old_text, new_text = old_dump(objects), new_dump(objects)
        for key in objects:
            failed = check("{} written directly".format(key), write_debug_value(key, objects[key], io.StringIO()), failed)
        found = same(replay_load(new_text, temp_dir, "objects_new"), replay_load(old_text, temp_dir, "objects_old"))
        failed = check("the replay loader rebuilds MinuteArray and array.array as from yaml.dump's dump{}".format(" - " + found if found else ""), not found, failed)

        # Anything else goes through yaml.dump, unchanged
        fallback = fallback_debug()
        for key in fallback:
            stream = io.StringIO()
            failed = check("{} left to yaml.dump, nothing written".format(key), not write_debug_value(key, fallback[key], stream) and stream.getvalue() == "", failed)
        fallback.pop("an_object")
        fallback.pop("with_cycle")
        fallback.pop("lone_surrogate")  # yaml.dump writes it as before; libyaml refuses that text either way
        failed = check("fallback keys dump exactly as before", new_dump(fallback) == old_dump(fallback), failed)

    print("**** Debug yaml direct writer tests {} ****".format("FAILED" if failed else "PASSED"))
    return failed
