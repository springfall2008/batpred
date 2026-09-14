# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for configurable log rotation (#5076) - the log_count apps.yaml setting, the
two-digit predbat.NN.log naming, and the migration path that reads the single-digit names an
older Predbat wrote.
"""

import os
import tempfile

from utils import (
    PREDBAT_LOG_COUNT_DEFAULT,
    PREDBAT_LOG_COUNT_MAX,
    PREDBAT_LOG_COUNT_MIN,
    predbat_log_count,
    predbat_log_file_prev,
    predbat_log_name,
    predbat_log_name_legacy,
    read_predbat_log,
    rotate_predbat_logs,
)


def _touch(name, content=""):
    """Create a file with known content, for asserting which file ended up where."""
    with open(name, "w", encoding="utf-8") as handle:
        handle.write(content)


def _rotate(args):
    """
    Run the rotation exactly as Hass.log() does, against the current working directory.

    Calls the same rotate_predbat_logs() Hass.log() calls, rather than a hand-copy of its loop -
    a copy can diverge from the code it is meant to be testing and still pass, which is exactly
    what let the off-by-one Copilot found on #5076 ship with a green test suite (the copy had the
    same bug, so it agreed with itself). Only the parts of Hass.log() this file does not otherwise
    exercise - closing/reopening the live predbat.log - are still done directly here.
    """
    max_logs = predbat_log_count(args) - 1
    rotate_predbat_logs(max_logs)
    os.rename("predbat.log", predbat_log_name(1))
    _touch("predbat.log", "live")


def run_log_rotation_tests(my_predbat):
    """
    Test configurable log rotation, two-digit naming and single-digit migration (#5076).
    """
    failed = False
    print("**** Running log_rotation tests ****")

    saved_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            print("Test 1: log_count defaults and clamps")
            for args, expected, why in (
                ({}, PREDBAT_LOG_COUNT_DEFAULT, "absent"),
                ({"log_count": 25}, 25, "in range"),
                ({"log_count": 1}, PREDBAT_LOG_COUNT_MIN, "below minimum"),
                ({"log_count": 500}, PREDBAT_LOG_COUNT_MAX, "above maximum"),
                ({"log_count": "30"}, 30, "numeric string"),
                ({"log_count": "banana"}, PREDBAT_LOG_COUNT_DEFAULT, "non-numeric"),
                ({"log_count": None}, PREDBAT_LOG_COUNT_DEFAULT, "null"),
                # YAML accepts non-finite numeric scalars (.inf/-.inf), which parse as a float
                # int() cannot convert - OverflowError, not caught by the (TypeError, ValueError)
                # this already guarded against (Copilot review on #5076).
                ({"log_count": float("inf")}, PREDBAT_LOG_COUNT_DEFAULT, "positive infinity"),
                ({"log_count": float("-inf")}, PREDBAT_LOG_COUNT_DEFAULT, "negative infinity"),
            ):
                actual = predbat_log_count(args)
                if actual != expected:
                    print("  ERROR: log_count {} ({}) should give {}, got {}".format(args, why, expected, actual))
                    failed = True

            print("Test 2: names are zero-padded to two digits")
            if predbat_log_name(1) != "predbat.01.log" or predbat_log_name(99) != "predbat.99.log":
                print("  ERROR: unexpected padded names: {!r} {!r}".format(predbat_log_name(1), predbat_log_name(99)))
                failed = True
            if predbat_log_name_legacy(1) != "predbat.1.log":
                print("  ERROR: unexpected legacy name: {!r}".format(predbat_log_name_legacy(1)))
                failed = True

            print("Test 3: a rotation shifts every log up one slot")
            _touch("predbat.log", "current")
            _touch(predbat_log_name(1), "one")
            _touch(predbat_log_name(2), "two")
            _rotate({"log_count": 10})
            for name, expected in ((predbat_log_name(1), "current"), (predbat_log_name(2), "one"), (predbat_log_name(3), "two")):
                if not os.path.isfile(name):
                    print("  ERROR: expected {} to exist after rotation".format(name))
                    failed = True
                elif open(name, encoding="utf-8").read() != expected:
                    print("  ERROR: {} should hold {!r}, got {!r}".format(name, expected, open(name, encoding="utf-8").read()))
                    failed = True

            print("Test 4: single-digit logs from an older Predbat migrate to two-digit")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            _touch(predbat_log_name_legacy(1), "old-one")
            _touch(predbat_log_name_legacy(2), "old-two")
            _rotate({"log_count": 10})
            if os.path.isfile(predbat_log_name_legacy(2)) or os.path.isfile(predbat_log_name_legacy(3)):
                print("  ERROR: single-digit files should have been renamed, found {}".format(sorted(os.listdir("."))))
                failed = True
            for name, expected in ((predbat_log_name(2), "old-one"), (predbat_log_name(3), "old-two")):
                if not os.path.isfile(name) or open(name, encoding="utf-8").read() != expected:
                    print("  ERROR: expected {} to hold {!r}, listing is {}".format(name, expected, sorted(os.listdir("."))))
                    failed = True

            print("Test 4b: a legacy single-digit log in the OLDEST kept slot is migrated, not stranded")
            # #5076 Copilot review: the shift previously only walked slots 1..max_logs-1, so a
            # legacy predbat.9.log sitting in the oldest kept slot (max_logs=9 for the default
            # log_count=10) was never a rename source - it does not collide with anything landing
            # on slot 9 from below, since that only writes the two-digit name. Left on disk under
            # the old name forever, invisible to predbat_log_file_prev()'s two-digit-first lookup.
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            _touch(predbat_log_name_legacy(9), "oldest-legacy")
            _rotate({"log_count": 10})  # max_logs = 9
            if os.path.isfile(predbat_log_name_legacy(9)):
                print("  ERROR: the legacy file in the oldest kept slot should not survive under its old name, found {}".format(sorted(os.listdir("."))))
                failed = True
            if os.path.isfile(predbat_log_name(10)) or os.path.isfile(predbat_log_name_legacy(10)):
                print("  ERROR: log_count=10 keeps only slots 1-9 plus the live log, found {}".format(sorted(os.listdir("."))))
                failed = True

            print("Test 5: logs beyond the configured count are removed")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            for num in range(1, 6):
                _touch(predbat_log_name(num), "log{}".format(num))
            _rotate({"log_count": 3})
            # Keeping 3 files total means the live log plus slots 01 and 02.
            if os.path.isfile(predbat_log_name(3)):
                print("  ERROR: slot 03 should have been removed with log_count 3, listing is {}".format(sorted(os.listdir("."))))
                failed = True
            remaining = sorted(name for name in os.listdir(".") if name != "predbat.log")
            if remaining != [predbat_log_name(1), predbat_log_name(2)]:
                print("  ERROR: expected only slots 01 and 02 to remain, got {}".format(remaining))
                failed = True

            print("Test 6: lowering log_count clears files stranded above the new count")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "current")
            for num in range(1, 21):
                _touch(predbat_log_name(num), "log{}".format(num))
            _rotate({"log_count": 4})
            stranded = [name for name in os.listdir(".") if name not in ("predbat.log", predbat_log_name(1), predbat_log_name(2), predbat_log_name(3))]
            if stranded:
                print("  ERROR: files above the new count should be removed, found {}".format(sorted(stranded)))
                failed = True

            print("Test 7: the viewer prefers the two-digit previous log")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "live-line")
            _touch(predbat_log_name(1), "padded-prev")
            if predbat_log_file_prev() != predbat_log_name(1):
                print("  ERROR: expected the padded name to win, got {!r}".format(predbat_log_file_prev()))
                failed = True
            data = read_predbat_log()
            if "padded-prev" not in data or "live-line" not in data:
                print("  ERROR: the viewer should show both logs, got {!r}".format(data))
                failed = True
            if data.index("padded-prev") > data.index("live-line"):
                print("  ERROR: the previous log should be prefixed, not appended, got {!r}".format(data))
                failed = True

            print("Test 8: the viewer falls back to a single-digit previous log after an upgrade")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "live-line")
            _touch(predbat_log_name_legacy(1), "legacy-prev")
            if predbat_log_file_prev() != predbat_log_name_legacy(1):
                print("  ERROR: expected the legacy name as fallback, got {!r}".format(predbat_log_file_prev()))
                failed = True
            data = read_predbat_log()
            if "legacy-prev" not in data:
                print("  ERROR: an upgrade must not lose the previous log, got {!r}".format(data))
                failed = True

            print("Test 9: no previous log at all is not an error")
            for name in os.listdir("."):
                os.remove(name)
            _touch("predbat.log", "only-line")
            if predbat_log_file_prev() is not None:
                print("  ERROR: expected no previous log, got {!r}".format(predbat_log_file_prev()))
                failed = True
            if read_predbat_log().strip() != "only-line":
                print("  ERROR: expected just the live log, got {!r}".format(read_predbat_log()))
                failed = True
    finally:
        os.chdir(saved_cwd)

    if failed:
        print("\n**** log_rotation tests: FAILED ****")
    else:
        print("\n**** log_rotation tests: PASSED ****")

    return failed
