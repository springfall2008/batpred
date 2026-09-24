# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on
"""Tests for the packed export limit encoding accessors.

An export limit is one double carrying three orthogonal signals - target SoC, export power and
mode. These tests pin the accessors in utils.py against the decode expressions that are written
out by hand in Prediction.run_prediction and prediction_kernel.cpp, so the two cannot drift.
"""

import random

from const import EXPORT_LIMIT_FREEZE, EXPORT_LIMIT_IDLE, EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE
from tests.test_infra import TestInverter
from userinterface import dump_debug_yaml, DEBUG_YAML_LOADER
import io
import yaml
from utils import export_mode_of, export_target_of, export_power_of, export_limit_sort_key, pack_export_limit, export_limits_to_stored, export_limits_from_stored


def test_export_encoding_roundtrip():
    """Every representable target/power pair survives a pack/unpack round trip"""
    failed = 0
    # The full 0-100 range, including the 99 and 100 the packed encoding had no room for
    for target in range(0, 101):
        for power in (1.0, 0.7, 0.5, 0.3):
            packed = pack_export_limit(EXPORT_MODE_TARGET, target, power)
            if export_mode_of(packed) != EXPORT_MODE_TARGET:
                print("ERROR: packed {} target {} power {} decoded as mode {}".format(packed, target, power, export_mode_of(packed)))
                failed += 1
            if export_target_of(packed) != target:
                print("ERROR: packed {} decoded target {} expected {}".format(packed, export_target_of(packed), target))
                failed += 1
            if abs(export_power_of(packed) - power) > 1e-9:
                print("ERROR: packed {} decoded power {} expected {}".format(packed, export_power_of(packed), power))
                failed += 1
    return failed


def test_export_encoding_modes():
    """The two reserved mode values decode as modes and carry no target"""
    failed = 0
    for mode, expected in ((EXPORT_MODE_FREEZE, EXPORT_LIMIT_FREEZE), (EXPORT_MODE_IDLE, EXPORT_LIMIT_IDLE)):
        packed = pack_export_limit(mode)
        # The sort key is the packed value the encoding used to be, still used for ordering and the
        # display paths - a limit is a tuple now, so this asks for it explicitly rather than by ==
        if export_limit_sort_key(packed) != expected:
            print("ERROR: mode {} packed to {} expected {}".format(mode, export_limit_sort_key(packed), expected))
            failed += 1
        if export_mode_of(packed) != mode:
            print("ERROR: mode {} decoded as {}".format(mode, export_mode_of(packed)))
            failed += 1
        # A mode has no target - returning None stops a caller using 99/100 as if it were one
        if export_target_of(packed) is not None:
            print("ERROR: mode {} reported a target {}".format(mode, export_target_of(packed)))
            failed += 1
        if export_power_of(packed) != 1.0:
            print("ERROR: mode {} reported power {} expected full rate".format(mode, export_power_of(packed)))
            failed += 1
    return failed


def test_export_encoding_matches_legacy_decode():
    """The accessors agree with the hand-written decode expressions they replace.

    prediction.py and prediction_kernel.cpp both spell the decode out inline; this pins the
    accessors to those expressions so a change to one without the other is caught here.
    """
    failed = 0
    random.seed(2)
    values = [99.0, 100.0, 98.999, 0.0, 47.3] + [random.uniform(0, 99) for _ in range(5000)]
    for value in values:
        # prediction.py:903 / prediction_kernel.cpp:949
        legacy_power = 1 - (value - int(value)) if value < EXPORT_LIMIT_FREEZE else 1.0
        if abs(export_power_of(value) - legacy_power) > 1e-12:
            print("ERROR: value {} power {} legacy {}".format(value, export_power_of(value), legacy_power))
            failed += 1
        # prediction.py:1074 - freeze is only ever the exact sentinel today
        legacy_freeze = value < EXPORT_LIMIT_IDLE and value == EXPORT_LIMIT_FREEZE
        if (export_mode_of(value) == EXPORT_MODE_FREEZE) != legacy_freeze:  # exact match, so these agree everywhere
            print("ERROR: value {} freeze {} legacy {}".format(value, export_mode_of(value) == EXPORT_MODE_FREEZE, legacy_freeze))
            failed += 1
        if (export_mode_of(value) == EXPORT_MODE_IDLE) != (value >= EXPORT_LIMIT_IDLE):
            print("ERROR: value {} idle disagreed with legacy".format(value))
            failed += 1
    return failed


def test_export_encoding_reserved_interval():
    """A value inside (99.0, 100.0) is read as a normal export, matching the more common convention.

    The packed encoding cannot produce this interval - reserving EXPORT_LIMIT_FREEZE while the
    fraction is live consumes it, so a low-power export to a 99% target is inexpressible. The
    codebase nonetheless disagrees about what such a value would mean: 11 sites test
    `== EXPORT_LIMIT_FREEZE` (99.5 is a normal export) and 15 test `< EXPORT_LIMIT_FREEZE` (99.5
    is not), and prediction.py holds both readings at once - line 900 will not force-export it
    and line 1074 will not freeze it, so it would silently idle.

    export_mode_of matches the sentinel exactly, preserving the first reading. This test pins
    that choice so the ambiguity is recorded rather than rediscovered; it is only truly fixable
    once the fields are split, because it exists solely because one number answers two questions.
    """
    failed = 0
    # Exactly the sentinel is a freeze
    if export_mode_of(99.0) != EXPORT_MODE_FREEZE:
        print("ERROR: 99.0 decoded as mode {} expected freeze".format(export_mode_of(99.0)))
        failed += 1
    # Just above it is not - it is a 99% target at reduced power
    if export_mode_of(99.5) != EXPORT_MODE_TARGET:
        print("ERROR: 99.5 decoded as mode {} expected target".format(export_mode_of(99.5)))
        failed += 1
    if export_target_of(99.5) != 99:
        print("ERROR: 99.5 target {} expected 99".format(export_target_of(99.5)))
        failed += 1
    if abs(export_power_of(99.5) - 0.5) > 1e-9:
        print("ERROR: 99.5 power {} expected 0.5".format(export_power_of(99.5)))
        failed += 1
    return failed


def test_export_limit_serialisation_roundtrip():
    """Every representable limit survives a store/load round trip, field for field.

    The stored form is a self-describing mapping rather than the packed float, so a saved plan or
    debug dump says "freeze" instead of 99.0. The round trip must rebuild the identical tuple,
    since the planner compares limits by equality.
    """
    failed = 0
    limits = [pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    # 0 to 100 inclusive: the whole representable range, not the 0-98 the packed encoding had room
    # for. clip_export_slots genuinely produces the top of it - a near-full battery with a derated
    # discharge rate clips a target up to 99 or 100 - and those used to be written out faithfully
    # and read back as an idle window, silently dropping the export from a restored plan.
    for target in range(0, 101):
        for power in (1.0, 0.7, 0.5, 0.3):
            limits.append(pack_export_limit(EXPORT_MODE_TARGET, target, power))

    restored = export_limits_from_stored(export_limits_to_stored(limits))
    if restored != limits:
        print("ERROR: round trip changed the limits: {} became {}".format(limits, restored))
        failed += 1

    # The stored form must be plain data any YAML tool can read
    for entry in export_limits_to_stored(limits):
        if type(entry) is not dict or type(entry["mode"]) is not str:
            print("ERROR: stored entry is not a plain mapping: {!r}".format(entry))
            failed += 1
            break
    return failed


def test_export_limit_reads_the_old_float_form():
    """Bare floats from before the mapping existed still load, decoded to fields.

    Debug dumps arrive from whatever version the reporter is running, so this is a permanent
    compatibility path rather than a migration.
    """
    failed = 0
    # 47.3 is a 47% target at 70% power, 99.0 a freeze and 100.0 an idle window - checked as fields
    # rather than by packing them back up, so a decode that lost the power would be caught
    expected = [pack_export_limit(EXPORT_MODE_TARGET, 47, 0.7), pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    if export_limits_from_stored([47.3, 99.0, 100.0]) != expected:
        print("ERROR: the old float form did not load unchanged: got {}".format(export_limits_from_stored([47.3, 99.0, 100.0])))
        failed += 1
    # A malformed entry becomes idle rather than raising - a bad limit must not stop a replay
    for bad in (
        None,
        "nonsense",
        {"mode": "bogus"},
        {},
        {"mode": "target", "target": "nonsense"},
        {"mode": "target", "target": 40, "power": "nonsense"},
        {"mode": "target", "target": -1, "power": 0.7},
        {"mode": "target", "target": 101, "power": 0.7},
        {"mode": "target", "target": 40, "power": -0.1},
        {"mode": "target", "target": 40, "power": 1.1},
    ):
        if export_mode_of(export_limits_from_stored([bad])[0]) != EXPORT_MODE_IDLE:
            print("ERROR: malformed entry {!r} did not fall back to idle".format(bad))
            failed += 1
    return failed


def test_export_limit_dumps_as_plain_yaml():
    """An export limit must serialise through the debug dumper without a Python object tag.

    create_debug_yaml sweeps __dict__ wholesale, and PyYAML tags a bare tuple as !!python/tuple,
    which yaml.safe_load refuses - the dump is an artefact people attach to bug reports. The
    dumper carries a representer writing a tuple as an ordinary sequence, and create_debug_yaml
    converts export_limits_best to the mapping form first; this checks both survive a round trip
    through the dumper the dump actually uses.
    """
    failed = 0
    limits = [pack_export_limit(EXPORT_MODE_TARGET, 47, 0.7), pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    document = io.StringIO()
    dump_debug_yaml({"export_limits_best": export_limits_to_stored(limits)}, document)
    document = document.getvalue()
    if "!!python" in document:
        print("ERROR: the dump carries a Python object tag:\n{}".format(document))
        failed += 1
    try:
        loaded = yaml.safe_load(document)
    except yaml.YAMLError as error:
        print("ERROR: safe_load rejected the dump: {}".format(error))
        return failed + 1
    restored = export_limits_from_stored(loaded["export_limits_best"])
    if restored != limits:
        print("ERROR: the dump did not round trip: {} became {}".format(limits, restored))
        failed += 1
    return failed


def test_clipped_high_target_survives_a_save_and_reload():
    """A target clipped to the top of the range round trips instead of becoming an idle window.

    clip_export_slots narrows an export target towards the SoC the simulation says is reachable, so
    a battery predicted to sit near full with a derated discharge rate clips to a 99% or 100%
    target. The validator used to reject anything at or above the old freeze sentinel, so
    export_limit_to_stored wrote those out faithfully and export_limit_from_stored read them back as
    idle - the export window silently disappeared when a plan was restored after a restart, or when
    a debug dump was replayed. test_clip_export_slots pairs this with the clip pass itself, so the
    two halves cannot drift back apart.
    """
    failed = 0
    for target in (99, 100):
        limit = pack_export_limit(EXPORT_MODE_TARGET, target, 1.0)
        restored = export_limits_from_stored(export_limits_to_stored([limit]))[0]
        if restored != limit:
            print("ERROR: a {}% target did not survive a store/load round trip: {} became {}".format(target, limit, restored))
            failed += 1
        if export_mode_of(restored) != EXPORT_MODE_TARGET:
            print("ERROR: a {}% target reloaded as mode {} rather than a target".format(target, export_mode_of(restored)))
            failed += 1
    # Still rejected above the representable range - the bound moved, it did not go away
    for target in (101, 1000, -1):
        restored = export_limits_from_stored([{"mode": "target", "target": target, "power": 1.0}])[0]
        if export_mode_of(restored) != EXPORT_MODE_IDLE:
            print("ERROR: an out-of-range target of {} was accepted rather than falling back to idle".format(target))
            failed += 1
    return failed


def test_the_marshaller_agrees_with_its_slow_path():
    """Packing a limit buffer with struct gives byte-identical results to filling the fields.

    The fast path packs each record with struct.Struct("@iid") and casts the bytes onto the ctypes
    array, which is only valid while the two layouts agree. prediction_kernel checks that at import
    and falls back to per-field assignment when they do not - a fallback no shipped platform takes,
    so nothing would exercise it if this did not. Both paths are run here and compared field for
    field, so the fallback cannot rot into being wrong by the time a platform needs it.
    """
    failed = 0
    import prediction_kernel

    if not prediction_kernel._EXPORT_LIMIT_STRUCT_USABLE:
        print("ERROR: struct packing was disabled on this platform - PkExportLimit and '@iid' disagree on layout")
        failed += 1

    limits = [pack_export_limit(EXPORT_MODE_TARGET, 47, 0.7), pack_export_limit(EXPORT_MODE_TARGET, 100, 1.0), pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    fast = prediction_kernel._build_export_limit_array(limits)
    prediction_kernel._EXPORT_LIMIT_STRUCT_USABLE = False
    try:
        slow = prediction_kernel._build_export_limit_array(limits)
    finally:
        prediction_kernel._EXPORT_LIMIT_STRUCT_USABLE = True

    for index in range(len(limits)):
        packed = (fast[index].mode, fast[index].target, fast[index].power)
        filled = (slow[index].mode, slow[index].target, slow[index].power)
        if packed != filled:
            print("ERROR: limit {} packed as {} but filled as {}".format(limits[index], packed, filled))
            failed += 1
    # The two modes carry no target or power of their own, and reach the kernel as 0 and full rate
    for index, limit in enumerate(limits):
        if export_mode_of(limit) == EXPORT_MODE_TARGET:
            continue
        if (fast[index].target, fast[index].power) != (0, 1.0):
            print("ERROR: mode {} marshalled as target {} power {}, expected 0 and full rate".format(export_mode_of(limit), fast[index].target, fast[index].power))
            failed += 1
    return failed


def test_the_debug_dump_stores_every_export_limit_list(my_predbat):
    """Both the plan's export limits and each inverter's own copy are written in the stored form.

    create_debug_yaml sweeps __dict__ wholesale, so an inverter's export_limits rides into the dump
    alongside the two Predbat-level lists. It used to be left as the raw tuples, which the dumper's
    representer writes as bare sequences - a reader could not tell them from a window count, and a
    replay restored them as lists, where export_mode_of compares a list against a float and raises.
    Every list in the dump has to be in the same self-describing form.
    """
    failed = 0
    limits = [pack_export_limit(EXPORT_MODE_TARGET, 47, 0.7), pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    # A throwaway inverter rather than reset_inverter, which rewrites most of the shared fixture -
    # this module runs second in the registry, ahead of nearly the whole suite
    inverter = TestInverter()
    inverter.export_limits = list(limits)
    saved = {"export_limits": my_predbat.export_limits, "export_limits_best": my_predbat.export_limits_best, "inverters": my_predbat.inverters}
    my_predbat.export_limits = list(limits)
    my_predbat.export_limits_best = list(limits)
    my_predbat.inverters = [inverter]
    try:
        # The loader read_debug_yaml itself uses. A whole dump carries objects safe_load will not
        # build (it is loaded back only by Predbat); that the export limits in it are plain data is
        # what test_export_limit_dumps_as_plain_yaml pins, and this is about which lists get written.
        document = yaml.load(my_predbat.create_debug_yaml(write_file=False), Loader=DEBUG_YAML_LOADER)
    except yaml.YAMLError as error:
        print("ERROR: the debug dump did not load back: {}".format(error))
        return failed + 1
    finally:
        my_predbat.export_limits = saved["export_limits"]
        my_predbat.export_limits_best = saved["export_limits_best"]
        my_predbat.inverters = saved["inverters"]

    stored = [("export_limits", document.get("export_limits")), ("export_limits_best", document.get("export_limits_best")), ("inverters[0].export_limits", document.get("inverters", [{}])[0].get("export_limits"))]
    for name, written in stored:
        if not isinstance(written, list) or len(written) != len(limits):
            print("ERROR: {} was not written as a list of {} limits: {!r}".format(name, len(limits), written))
            failed += 1
            continue
        if any(not isinstance(entry, dict) or "mode" not in entry for entry in written):
            print("ERROR: {} was not written in the self-describing mapping form: {!r}".format(name, written))
            failed += 1
            continue
        if export_limits_from_stored(written) != limits:
            print("ERROR: {} did not round trip: {!r} became {}".format(name, written, export_limits_from_stored(written)))
            failed += 1
    return failed


def run_export_encoding_tests(my_predbat=None):
    """Run all export limit encoding tests"""
    failed = 0
    print("**** Running export encoding tests ****")
    failed += test_export_encoding_roundtrip()
    failed += test_export_encoding_modes()
    failed += test_export_encoding_matches_legacy_decode()
    failed += test_export_encoding_reserved_interval()
    failed += test_export_limit_serialisation_roundtrip()
    failed += test_export_limit_reads_the_old_float_form()
    failed += test_export_limit_dumps_as_plain_yaml()
    failed += test_clipped_high_target_survives_a_save_and_reload()
    failed += test_the_marshaller_agrees_with_its_slow_path()
    if my_predbat is not None:
        failed += test_the_debug_dump_stores_every_export_limit_list(my_predbat)
    if not failed:
        print("Test: export limit encoding accessors match the hand-written decode they replace")
    return failed
