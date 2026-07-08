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
            for time_str, expect in valid_cases:
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

    print("**** str2time tests completed ****")
    return failed
