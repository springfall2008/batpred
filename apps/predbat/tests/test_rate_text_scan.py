# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def test_rate_text_scan(my_predbat):
    """
    Test rate_text_scan across a multi-band tariff, checking that each band's
    reported min/max range covers exactly the rates within that band and not
    a neighbouring one.
    """
    failed = False
    print("**** Testing rate_text_scan ****")

    # Snapshot every field this test overwrites below and restore it in finally - none of
    # this was being restored, so it leaked a shrunk 9-minute horizon and a tiny synthetic
    # rate table into every later test sharing this fixture (#5079).
    original_minutes_now = my_predbat.minutes_now
    original_end_record = my_predbat.end_record
    original_forecast_minutes = my_predbat.forecast_minutes
    original_rate_min = my_predbat.rate_min
    original_rate_max = my_predbat.rate_max
    original_rate_import = my_predbat.rate_import

    try:
        my_predbat.minutes_now = 0
        my_predbat.end_record = 9
        my_predbat.forecast_minutes = 9
        my_predbat.rate_min = 4.0
        my_predbat.rate_max = 43.09

        # Band A (cheap, minutes 0-2): varies 4.0/10.0/6.0
        # Band B (expensive, minutes 3-5): varies 20.0/28.7/22.0
        # Band C (very expensive, minutes 6-8): flat 43.1
        my_predbat.rate_import = {
            0: 4.0,
            1: 10.0,
            2: 6.0,
            3: 20.0,
            4: 28.7,
            5: 22.0,
            6: 43.1,
            7: 43.1,
            8: 43.1,
        }

        result = my_predbat.rate_text_scan(export=False)

        expected = [
            {"start": 0, "end": 3, "rate": "cheap", "range": "(4.0p - 10.0p)"},
            {"start": 3, "end": 6, "rate": "expensive", "range": "(20.0p - 28.7p)"},
            {"start": 6, "end": 9, "rate": "very expensive", "range": "(43.1p)"},
        ]

        if result != expected:
            print(f"  ERROR: rate_text_scan mismatch\n  got:      {result}\n  expected: {expected}")
            failed = True
    finally:
        my_predbat.minutes_now = original_minutes_now
        my_predbat.end_record = original_end_record
        my_predbat.forecast_minutes = original_forecast_minutes
        my_predbat.rate_min = original_rate_min
        my_predbat.rate_max = original_rate_max
        my_predbat.rate_import = original_rate_import

    print("**** rate_text_scan tests completed ****")
    return failed
