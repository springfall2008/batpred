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

from const import EXPORT_LIMIT_FREEZE, EXPORT_LIMIT_IDLE, EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, FULL_EXPORT_POWER, LOW_EXPORT_POWER_LEVELS
from utils import export_mode_of, export_target_of, export_power_of, pack_export_limit, export_limit_exports_no_battery, export_limits_to_stored, export_limits_from_stored


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
        if packed != expected:
            print("ERROR: mode {} packed to {} expected {}".format(mode, packed, expected))
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


def test_export_limit_exports_no_battery():
    """The no-battery predicate matches the `>= EXPORT_LIMIT_FREEZE` expression it replaces.

    plan.py's trim pass exempts off/freeze windows from its earlier-start check, because they
    discharge no battery and force the start back to the full window. It asked that as an
    ordering comparison, which holds only because both reserved values sort above every real
    target. Pinning the predicate to that expression keeps the behaviour identical while the
    encoding still packs a fraction; when mode becomes its own field this becomes a membership
    test over modes and the ordering stops mattering.
    """
    failed = 0
    random.seed(3)
    values = [0.0, 47.3, 98.0, 98.999, 99.0, 99.5, 100.0] + [random.uniform(0, 101) for _ in range(5000)]
    for value in values:
        if export_limit_exports_no_battery(value) != (value >= EXPORT_LIMIT_FREEZE):
            print("ERROR: value {} predicate {} expected {}".format(value, export_limit_exports_no_battery(value), value >= EXPORT_LIMIT_FREEZE))
            failed += 1
    # The two modes that export no battery, and a normal target that does
    if not export_limit_exports_no_battery(pack_export_limit(EXPORT_MODE_FREEZE)):
        print("ERROR: a freeze should export no battery")
        failed += 1
    if not export_limit_exports_no_battery(pack_export_limit(EXPORT_MODE_IDLE)):
        print("ERROR: an idle window should export no battery")
        failed += 1
    if export_limit_exports_no_battery(pack_export_limit(EXPORT_MODE_TARGET, 50, 1.0)):
        print("ERROR: a real target exports battery and must not be exempt")
        failed += 1
    return failed


def test_export_limit_serialisation_roundtrip():
    """Every representable limit survives a store/load round trip exactly.

    The stored form is a self-describing mapping rather than the packed float, so a saved plan
    or debug dump says "freeze" instead of 99.0. The round trip must still rebuild the identical
    packed value, since the planner compares limits by equality.
    """
    failed = 0
    limits = [pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    for target in range(0, 99):
        for power in (1.0, 0.7, 0.5, 0.3):
            limits.append(pack_export_limit(EXPORT_MODE_TARGET, target, power))

    restored = export_limits_from_stored(export_limits_to_stored(limits))
    if [float(value) for value in restored] != [float(value) for value in limits]:
        print("ERROR: round trip changed the limits")
        failed += 1

    # The stored form must be plain data - a float subclass would serialise as a Python object
    # tag that only Predbat can read, and yaml.safe_dump refuses it outright
    for entry in export_limits_to_stored(limits):
        if type(entry) is not dict or type(entry["mode"]) is not str:
            print("ERROR: stored entry is not a plain mapping: {!r}".format(entry))
            failed += 1
            break
    return failed


def test_export_limit_reads_the_old_float_form():
    """Bare floats from before the mapping existed still load.

    Debug dumps arrive from whatever version the reporter is running, so this is a permanent
    compatibility path rather than a migration.
    """
    failed = 0
    if [float(value) for value in export_limits_from_stored([47.3, 99.0, 100.0])] != [47.3, 99.0, 100.0]:
        print("ERROR: the old float form did not load unchanged")
        failed += 1
    # A malformed entry becomes idle rather than raising - a bad limit must not stop a replay
    for bad in (None, "nonsense", {"mode": "bogus"}, {}):
        if export_mode_of(export_limits_from_stored([bad])[0]) != EXPORT_MODE_IDLE:
            print("ERROR: malformed entry {!r} did not fall back to idle".format(bad))
            failed += 1
    return failed


def test_export_ladder_rungs_are_unchanged():
    """The (mode, power) ladder produces exactly the packed values the float ladder did.

    optimise_export used to iterate a list mixing two kinds of thing - two modes and four power
    levels - and encode each by clamping the integer part to the SoC floor and re-attaching the
    rung's fraction. This pins the rewritten ladder to those values so the rewrite cannot have
    quietly dropped or reordered a rung, which the plan goldens alone would not catch if the
    dropped rung never won.
    """
    failed = 0
    for floor in (0, 13, 50, 98):
        # What the old float ladder produced: max(floor, int(rung)) + fraction
        old_rungs = [EXPORT_LIMIT_IDLE, EXPORT_LIMIT_FREEZE, 0.0, 0.3, 0.5, 0.7]
        expected = [max(floor, int(rung)) + rung - int(rung) for rung in old_rungs]

        new_rungs = [(EXPORT_MODE_IDLE, FULL_EXPORT_POWER), (EXPORT_MODE_FREEZE, FULL_EXPORT_POWER), (EXPORT_MODE_TARGET, FULL_EXPORT_POWER)]
        new_rungs += [(EXPORT_MODE_TARGET, power) for power in LOW_EXPORT_POWER_LEVELS]
        actual = [float(pack_export_limit(mode, floor, power)) for mode, power in new_rungs]

        if [round(value, 9) for value in actual] != [round(value, 9) for value in expected]:
            print("ERROR: floor {} ladder {} expected {}".format(floor, actual, expected))
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
    failed += test_export_limit_exports_no_battery()
    failed += test_export_limit_serialisation_roundtrip()
    failed += test_export_limit_reads_the_old_float_form()
    failed += test_export_ladder_rungs_are_unchanged()
    if not failed:
        print("Test: export limit encoding accessors match the hand-written decode they replace")
    return failed
