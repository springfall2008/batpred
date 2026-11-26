# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""
Tests that replaying a debug dump never runs code the dump names.

Debug dumps are attached to public bug reports and replayed by maintainers and the triage bot, so
DEBUG_YAML_LOADER must be a safe loader: a !!python/object/apply, /new, /name or /module tag must be
refused, not imported and called. The Predbat objects real dumps carry must still come back as
instances, and any other !!python/object: as a plain dict that the replay warns about.
"""

import os
import tempfile

import numpy as np
import yaml

import userinterface
from control_ledger import ControlLedger
from control_monitor import ControlMonitor
from userinterface import DEBUG_YAML_LOADER, load_debug_yaml_file
from utils import MinuteArray

# Set by probe() if anything ever calls it; a dump that names it must leave it at zero
PROBE_CALLS = []


def probe(*args):
    """Record a call. Stands in for os.system in a crafted dump, so the test can see whether it ran."""
    PROBE_CALLS.append(args)
    return "called"


CALL_FORMS = {
    "apply": "value: !!python/object/apply:tests.test_debug_yaml_safe_load.probe [boom]\n",
    "new": "value: !!python/object/new:tests.test_debug_yaml_safe_load.probe [boom]\n",
    "name": "value: !!python/name:tests.test_debug_yaml_safe_load.probe\n",
    "module": "value: !!python/module:os\n",
}

UNKNOWN_OBJECT_DUMP = """minutes_now: 5
mystery: !!python/object:somewhere.Mystery
  colour: blue
  sizes: [1, 2]
CONFIG_ITEMS: []
"""


def test_crafted_dump_is_refused():
    """Each Python call form is refused by the replay loader, though PyYAML's unsafe loader would run it."""
    failed = False

    # First confirm the hazard is real, so a refusal below means the loader did something
    del PROBE_CALLS[:]
    yaml.load(CALL_FORMS["apply"], Loader=getattr(yaml, "CLoader", yaml.UnsafeLoader))
    if not PROBE_CALLS:
        print("ERROR: PyYAML's unsafe loader did not call the probe, so this test no longer shows the hazard")
        failed = True

    for form, text in CALL_FORMS.items():
        del PROBE_CALLS[:]
        try:
            value = yaml.load(text, Loader=DEBUG_YAML_LOADER)
            print("ERROR: the replay loader accepted a !!python/{} tag and built {!r}".format(form, value))
            failed = True
        except yaml.YAMLError:
            pass
        if PROBE_CALLS:
            print("ERROR: loading a !!python/{} tag called the function it names".format(form))
            failed = True
    return failed


def test_crafted_dump_file_is_refused(my_predbat):
    """read_debug_yaml() refuses a crafted file rather than running it - the path triage_replay.py and --debug_file take."""
    failed = False
    del PROBE_CALLS[:]
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "predbat_debug_crafted.yaml")
        with open(path, "w") as file:
            file.write(CALL_FORMS["apply"])
        try:
            my_predbat.read_debug_yaml(path)
            print("ERROR: read_debug_yaml() accepted a dump carrying !!python/object/apply")
            failed = True
        except yaml.YAMLError:
            pass
    if PROBE_CALLS:
        print("ERROR: read_debug_yaml() called the function a crafted dump names")
        failed = True
    return failed


def test_unknown_object_loads_as_dict(my_predbat):
    """An object of a class the loader does not know comes back as a dict of its attributes, and the replay names it."""
    failed = False
    debug = yaml.load(UNKNOWN_OBJECT_DUMP, Loader=DEBUG_YAML_LOADER)
    if debug.get("mystery") != {"colour": "blue", "sizes": [1, 2]}:
        print("ERROR: expected an unknown object to load as a dict of its attributes, got {!r}".format(debug.get("mystery")))
        failed = True

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "predbat_debug_unknown.yaml")
        with open(path, "w") as file:
            file.write(UNKNOWN_OBJECT_DUMP)
        seen = set()
        load_debug_yaml_file(path, seen)
        if seen != {"somewhere.Mystery"}:
            print("ERROR: expected the unknown tag to be reported, got {}".format(seen))
            failed = True

        # The warning must also come through the parse cache the unit tests switch on
        saved_cache = userinterface.DEBUG_YAML_PARSE_CACHE
        try:
            userinterface.DEBUG_YAML_PARSE_CACHE = {}
            load_debug_yaml_file(path)
            seen = set()
            load_debug_yaml_file(path, seen)
            if seen != {"somewhere.Mystery"}:
                print("ERROR: a cached parse lost the unknown tag, got {}".format(seen))
                failed = True
        finally:
            userinterface.DEBUG_YAML_PARSE_CACHE = saved_cache

        logged = []
        original_log = my_predbat.log
        try:
            my_predbat.log = logged.append
            my_predbat.read_debug_yaml(path)
        finally:
            my_predbat.log = original_log
        if not any("somewhere.Mystery" in line and line.startswith("Warn:") for line in logged):
            print("ERROR: read_debug_yaml() did not warn about the object it loaded as a dict, logged {}".format(logged))
            failed = True
    return failed


def test_fresh_dump_leaves_out_control_state(my_predbat):
    """A fresh dump carries neither control object, and loads with no object the replay loader does not know."""
    failed = False
    text = my_predbat.create_debug_yaml(write_file=False)
    loader = DEBUG_YAML_LOADER(text)
    try:
        debug = loader.get_single_data()
        unknown = set(loader.unknown_object_tags)
    finally:
        loader.dispose()
    if unknown:
        print("ERROR: a fresh dump holds objects the replay loader does not know: {} - add them to DEBUG_YAML_OBJECT_CLASSES or DEBUG_EXCLUDE_LIST".format(sorted(unknown)))
        failed = True
    for key in ("control_ledger", "control_monitor"):
        if key in debug:
            print("ERROR: a fresh dump carries {}, which a replay never reads".format(key))
            failed = True
    return failed


def test_older_dump_restores_control_objects(my_predbat):
    """A dump written before the control objects were excluded still loads them as instances carrying their state."""
    failed = False
    ledger = ControlLedger()
    ledger.cycle = 7
    monitor = ControlMonitor()
    monitor.total = 3
    text = yaml.dump({"control_ledger": ledger, "control_monitor": monitor}, Dumper=yaml.Dumper)
    for tag in ("control_ledger.ControlLedger", "control_monitor.ControlMonitor"):
        if "!!python/object:" + tag not in text:
            print("ERROR: the older dump fixture does not carry {}, so this test does not exercise it".format(tag))
            failed = True
    debug = yaml.load(text, Loader=DEBUG_YAML_LOADER)
    for key, original in (("control_ledger", ledger), ("control_monitor", monitor)):
        value = debug.get(key)
        if type(value) is not type(original) or value.__dict__ != original.__dict__:
            print("ERROR: expected {} to load as a {} with its state, got {!r}".format(key, type(original).__name__, value))
            failed = True
    return failed


def test_tuples_and_numeric_numpy_only():
    """A !!python/tuple loads as a tuple; a numpy scalar loads only when its dtype is numeric."""
    failed = False
    value = yaml.load("value: !!python/tuple [1, 2]\n", Loader=DEBUG_YAML_LOADER)["value"]
    if value != (1, 2) or not isinstance(value, tuple):
        print("ERROR: expected the tuple (1, 2), got {!r}".format(value))
        failed = True

    # A plain yaml.Dumper writes a numpy value the way older Predbat dumps did
    number = yaml.load(yaml.dump({"value": np.float64(2.5)}, Dumper=yaml.Dumper), Loader=DEBUG_YAML_LOADER)["value"]
    if number != 2.5 or not isinstance(number, np.floating):
        print("ERROR: expected the numpy value 2.5, got {!r}".format(number))
        failed = True

    object_dtype = "value: !!python/object/apply:numpy.dtype\n  args: [O8, false, true]\n"
    try:
        yaml.load(object_dtype, Loader=DEBUG_YAML_LOADER)
        print("ERROR: the replay loader built an object numpy dtype")
        failed = True
    except yaml.YAMLError:
        pass
    return failed


def test_minute_array_round_trips():
    """A MinuteArray, which dumps taken after a plan cycle carry, loads back with its values; a non-numeric array is refused."""
    failed = False
    series = MinuteArray({0: 1.5, 1: 2.0, 2: -3.25}, 3)
    loaded = yaml.load(yaml.dump({"value": series}, Dumper=yaml.Dumper), Loader=DEBUG_YAML_LOADER)["value"]
    if not isinstance(loaded, MinuteArray) or list(loaded._data) != [1.5, 2.0, -3.25] or loaded.get(2) != -3.25:
        print("ERROR: expected the MinuteArray [1.5, 2.0, -3.25] back, got {!r}".format(loaded))
        failed = True

    for text in ("value: !!python/object/apply:array.array [w, [abc]]\n", "value: !!python/object/apply:array.array [d, [one]]\n"):
        try:
            yaml.load(text, Loader=DEBUG_YAML_LOADER)
            print("ERROR: the replay loader built a non-numeric array from {!r}".format(text))
            failed = True
        except yaml.YAMLError:
            pass
    return failed


def run_debug_yaml_safe_load_tests(my_predbat):
    """Run the debug dump safe-load tests, returning True on failure."""
    print("**** Testing that replaying a debug dump never runs code the dump names ****")
    failed = False
    failed |= test_crafted_dump_is_refused()
    failed |= test_crafted_dump_file_is_refused(my_predbat)
    failed |= test_unknown_object_loads_as_dict(my_predbat)
    failed |= test_fresh_dump_leaves_out_control_state(my_predbat)
    failed |= test_older_dump_restores_control_objects(my_predbat)
    failed |= test_tuples_and_numeric_numpy_only()
    failed |= test_minute_array_round_trips()
    if not failed:
        print("PASS: debug dumps load safely")
    return failed
