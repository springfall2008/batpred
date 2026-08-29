# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for get_arg()'s numeric coercion when the resolved value is missing (None) rather than
malformed.

Per-inverter args are lists indexed by inverter id, but nothing guarantees the list is as long as
num_inverters. set_arg(arg, value, index=i) grows the list only to i+1, so an arg first written by
inverter 0 - e.g. inverter.py's battery_scaling_auto caching a measured soc_max - leaves a
single-element list that inverter 1 then reads out of range. resolve_arg() correctly returns None
there (and logs "Out of range index ..."), which is precisely what the caller's default is for.

Coercing that None used to raise TypeError and be reported via record_status(had_errors=True),
pinning "Warn: Return bad float value None from soc_max" on the status sensor and ending every
5-minute run as "Read-Only with Errors" - for a gap the caller already handles (inverter.py falls
back to the 8 kWh default). A value that is present but unparseable ("unavailable", "abc") is a
genuine fault and must still be reported.
"""


def test_get_arg_missing_index_uses_default_quietly(my_predbat):
    """A too-short per-inverter list returns the default without flagging an error, while a
    present-but-unparseable value still warns and sets had_errors."""
    failed = False
    print("**** Testing get_arg numeric coercion for missing vs malformed values ****")

    # my_predbat is a shared fixture across the whole test run - save everything this test touches
    # so it can be restored afterward regardless of outcome, or later tests get a poisoned instance.
    had_soc_max_arg = "soc_max" in my_predbat.args
    original_soc_max = my_predbat.args.get("soc_max")
    had_scaling_arg = "battery_scaling_last_known" in my_predbat.args
    original_scaling = my_predbat.args.get("battery_scaling_last_known")
    original_had_errors = my_predbat.had_errors
    original_status = my_predbat.current_status

    try:
        print("Test: float arg, index past the end of the list - returns the default, stays silent")
        my_predbat.args["soc_max"] = [25.68]
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        value = my_predbat.get_arg("soc_max", default=0.0, index=1)
        if value != 0.0:
            print("  ERROR: expected the default 0.0 for an out-of-range index, got {!r}".format(value))
            failed = True
        if my_predbat.had_errors:
            print("  ERROR: a missing per-inverter value must not set had_errors - it is what the default is for")
            failed = True
        if my_predbat.current_status != "":
            print("  ERROR: a missing per-inverter value must not pin the status sensor, got {!r}".format(my_predbat.current_status))
            failed = True

        print("Test: float arg, index within the list - returns the configured value")
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        value = my_predbat.get_arg("soc_max", default=0.0, index=0)
        if value != 25.68:
            print("  ERROR: expected the configured 25.68 for index 0, got {!r}".format(value))
            failed = True
        if my_predbat.had_errors:
            print("  ERROR: a valid value must not set had_errors")
            failed = True

        print("Test: float arg, present but unparseable - still warns and flags an error")
        my_predbat.args["soc_max"] = [25.68, "unavailable"]
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        value = my_predbat.get_arg("soc_max", default=0.0, index=1)
        if value != 0.0:
            print("  ERROR: expected the default 0.0 for an unparseable value, got {!r}".format(value))
            failed = True
        if not my_predbat.had_errors:
            print("  ERROR: expected had_errors to be set for a present-but-unparseable value")
            failed = True
        if "soc_max" not in my_predbat.current_status:
            print("  ERROR: expected the warning to be recorded to current_status, got {!r}".format(my_predbat.current_status))
            failed = True

        print("Test: int arg, index past the end of the list - returns the default, stays silent")
        my_predbat.args["battery_scaling_last_known"] = [1]
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        value = my_predbat.get_arg("battery_scaling_last_known", default=2, index=1)
        if value != 2:
            print("  ERROR: expected the int default 2 for an out-of-range index, got {!r}".format(value))
            failed = True
        if my_predbat.had_errors:
            print("  ERROR: a missing per-inverter int value must not set had_errors")
            failed = True
        if my_predbat.current_status != "":
            print("  ERROR: a missing per-inverter int value must not pin the status sensor, got {!r}".format(my_predbat.current_status))
            failed = True

        print("Test: int arg, present but unparseable - still warns and flags an error")
        my_predbat.args["battery_scaling_last_known"] = [1, "unavailable"]
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        value = my_predbat.get_arg("battery_scaling_last_known", default=2, index=1)
        if value != 2:
            print("  ERROR: expected the int default 2 for an unparseable value, got {!r}".format(value))
            failed = True
        if not my_predbat.had_errors:
            print("  ERROR: expected had_errors to be set for a present-but-unparseable int value")
            failed = True

    finally:
        if had_soc_max_arg:
            my_predbat.args["soc_max"] = original_soc_max
        else:
            my_predbat.args.pop("soc_max", None)
        if had_scaling_arg:
            my_predbat.args["battery_scaling_last_known"] = original_scaling
        else:
            my_predbat.args.pop("battery_scaling_last_known", None)
        my_predbat.had_errors = original_had_errors
        my_predbat.current_status = original_status

    if not failed:
        print("**** get_arg missing-index tests PASSED ****")
    return failed
