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
from userinterface import dump_debug_yaml
import io
import yaml
from utils import export_mode_of, export_target_of, export_power_of, export_limit_sort_key, pack_export_limit, export_limits_to_stored, export_limits_from_stored


def test_export_encoding_roundtrip():
    """Every representable target/power pair survives a pack/unpack round trip"""
    failed = 0
    for target in range(0, 99):
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
    for target in range(0, 99):
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
        {"mode": "target", "target": 99, "power": 0.7},
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
    if not failed:
        print("Test: export limit encoding accessors match the hand-written decode they replace")
    return failed
