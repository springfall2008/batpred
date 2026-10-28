# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone, timedelta


def test_str2time(my_predbat):
    """
    Test the str2time function from utils in both fromisoformat and strptime modes
    """
    failed = False
    print("**** Testing str2time function ****")

    import utils
    from utils import str2time, str2time_strptime

    valid_cases = [
        # (input string, expected datetime)
        ("2024-09-07T15:40:09.799567+00:00", datetime(2024, 9, 7, 15, 40, 9, 799567, tzinfo=timezone.utc)),
        ("2024-09-07T15:40:09.799567+0100", datetime(2024, 9, 7, 15, 40, 9, 799567, tzinfo=timezone(timedelta(hours=1)))),
        ("2024-09-07T15:40:09+00:00", datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone.utc)),
        ("2024-09-07T15:40:09+0000", datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone.utc)),
        ("2024-09-07T15:40:09Z", datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone.utc)),
        ("2024-09-07T15:40:09.799567Z", datetime(2024, 9, 7, 15, 40, 9, 799567, tzinfo=timezone.utc)),
        ("2024-09-07 15:40:09+00:00", datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone.utc)),
        ("2024-09-07 15:40:09+0100", datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone(timedelta(hours=1)))),
        ("2024-09-07T15:40:09-05:00", datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone(timedelta(hours=-5)))),
    ]

    invalid_cases = [
        "2024-09-07T15:40:09",  # Naive (no UTC offset) must raise like strptime with %z does
        "2024-09-07",
        "junk",
        "",
    ]

    save_flag = utils.STR2TIME_USE_FROMISOFORMAT
    try:
        for mode in [True, False]:
            utils.STR2TIME_USE_FROMISOFORMAT = mode
            mode_name = "fromisoformat" if mode else "strptime"

            print(f"Test 1 ({mode_name}): valid time strings parse to the expected datetime")
            for time_str, expect in valid_cases:
                result = str2time(time_str)
                if result != expect:
                    print(f"ERROR: Test 1 ({mode_name}) failed - str2time({time_str!r}) expected {expect}, got {result}")
                    failed = True

            print(f"Test 2 ({mode_name}): both implementations agree")
            for time_str, _expect in valid_cases:
                result = str2time(time_str)
                legacy = str2time_strptime(time_str)
                if result != legacy:
                    print(f"ERROR: Test 2 ({mode_name}) failed - str2time({time_str!r}) gave {result} but strptime gave {legacy}")
                    failed = True

            print(f"Test 3 ({mode_name}): invalid or naive time strings raise ValueError")
            for time_str in invalid_cases:
                try:
                    result = str2time(time_str)
                    print(f"ERROR: Test 3 ({mode_name}) failed - str2time({time_str!r}) expected ValueError, got {result}")
                    failed = True
                except ValueError:
                    pass
    finally:
        utils.STR2TIME_USE_FROMISOFORMAT = save_flag

    failed |= test_strptime_aware()
    failed |= test_no_raw_offset_strptime()

    print("**** str2time tests completed ****")
    return failed


def offset_strptime_calls(source, constants):
    """
    Return the line numbers of strptime() calls in source whose format is a string literal, or a name
    bound to one in constants or at module level, that contains %z
    """
    import ast

    tree = ast.parse(source)
    known = dict(constants)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    known[target.id] = node.value.value
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "strptime" and len(node.args) >= 2):
            continue
        fmt = node.args[1]
        if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
            fmt_text = fmt.value
        elif isinstance(fmt, ast.Name):
            fmt_text = known.get(fmt.id, "")
        else:
            fmt_text = ""
        if "%z" in fmt_text:
            lines.append(node.lineno)
    return lines


def test_no_raw_offset_strptime():
    """
    Test that no production code calls strptime() directly with a %z format - on Python 3.15 that returns
    a naive datetime for a string with no offset, so it must go through strptime_aware() instead
    """
    import glob
    import os
    import const

    failed = False
    print("Test 6: no production strptime() call uses a %z format directly")
    constants = {name: value for name, value in vars(const).items() if isinstance(value, str)}
    source_dir = os.path.dirname(const.__file__)

    # The scan must find a raw call when there is one
    if offset_strptime_calls('x = datetime.strptime(s, TIME_FORMAT)\ny = datetime.strptime(s, "%H:%M")\n', constants) != [1]:
        print("ERROR: Test 6 failed - the scan did not find a raw strptime call with TIME_FORMAT")
        failed = True

    for path in sorted(glob.glob(os.path.join(source_dir, "*.py"))):
        with open(path, encoding="utf-8") as handle:
            lines = offset_strptime_calls(handle.read(), constants)
        for line in lines:
            print(f"ERROR: Test 6 failed - {os.path.basename(path)}:{line} calls strptime with a %z format; use utils.strptime_aware()")
            failed = True
    return failed


class Python315Datetime(datetime):
    """
    datetime whose strptime behaves like Python 3.15's: a %z format accepts a string with no offset
    and returns a naive datetime, where 3.14 and earlier raise ValueError
    """

    @classmethod
    def strptime(cls, date_string, fmt):
        """Parse as before, retrying without %z when the offset is missing"""
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            if "%z" not in fmt:
                raise
            return datetime.strptime(date_string, fmt.replace("%z", ""))


def test_strptime_aware():
    """
    Test strptime_aware(), and that str2time() and a call site still reject a string with no UTC
    offset when strptime itself accepts one (Python 3.15)
    """
    failed = False
    import utils
    from utils import strptime_aware, str2time
    from octopus import parse_date_time

    print("Test 4: strptime_aware returns an aware datetime and rejects a naive one")
    result = strptime_aware("2024-09-07T15:40:09+0100", "%Y-%m-%dT%H:%M:%S%z")
    if result != datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone(timedelta(hours=1))):
        print(f"ERROR: Test 4 failed - expected 15:40:09+01:00, got {result}")
        failed = True
    # A format without %z always parses naive, so this exercises the check on any Python version
    try:
        result = strptime_aware("2024-09-07T15:40:09", "%Y-%m-%dT%H:%M:%S")
        print(f"ERROR: Test 4 failed - a naive result should raise ValueError, got {result}")
        failed = True
    except ValueError:
        pass

    print("Test 5: with Python 3.15's strptime, naive strings still raise and offsets still parse")
    import octopus

    save_datetime = utils.datetime
    save_octopus_datetime = octopus.datetime
    save_flag = utils.STR2TIME_USE_FROMISOFORMAT
    utils.datetime = Python315Datetime
    octopus.datetime = Python315Datetime
    try:
        if utils.datetime.strptime("2024-09-07T15:40:09", "%Y-%m-%dT%H:%M:%S%z").tzinfo is not None:
            print("ERROR: Test 5 failed - the simulated Python 3.15 strptime did not return a naive datetime")
            failed = True
        for mode in [True, False]:
            utils.STR2TIME_USE_FROMISOFORMAT = mode
            mode_name = "fromisoformat" if mode else "strptime"
            try:
                result = str2time("2024-09-07T15:40:09")
                print(f"ERROR: Test 5 ({mode_name}) failed - str2time of a naive string expected ValueError, got {result}")
                failed = True
            except ValueError:
                pass
            for time_str in ["2024-09-07T15:40:09+0100", "2024-09-07 15:40:09+00:00", "2024-09-07T15:40:09.799567Z"]:
                result = str2time(time_str)
                if result.tzinfo is None:
                    print(f"ERROR: Test 5 ({mode_name}) failed - str2time({time_str!r}) lost its offset: {result}")
                    failed = True
        result = parse_date_time("2024-09-07T15:40:09")
        if result is not None:
            print(f"ERROR: Test 5 failed - octopus parse_date_time of a naive string expected None, got {result}")
            failed = True
        result = parse_date_time("2024-09-07T15:40:09Z")
        if result != datetime(2024, 9, 7, 15, 40, 9, tzinfo=timezone.utc):
            print(f"ERROR: Test 5 failed - octopus parse_date_time expected 15:40:09 UTC, got {result}")
            failed = True
    finally:
        utils.datetime = save_datetime
        octopus.datetime = save_octopus_datetime
        utils.STR2TIME_USE_FROMISOFORMAT = save_flag

    return failed
