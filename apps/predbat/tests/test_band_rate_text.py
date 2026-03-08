# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def test_band_rate_text(my_predbat):
    """
    Test band_rate_text with various tariff profiles:
    - Flat rate (single price, rate_min == rate_max)
    - Cosy Octopus (3 distinct import rates)
    - Flux (import and export with varying rates)
    """
    failed = False
    print("**** Testing band_rate_text ****")

    # --- Flat rate tariff (e.g. fixed 24.5p import, fixed 15p export) ---
    print("Test: Flat rate import")
    my_predbat.rate_min = 24.5
    my_predbat.rate_max = 24.5
    my_predbat.rate_export_min = 15.0
    my_predbat.rate_export_max = 15.0

    result = my_predbat.band_rate_text(24.5)
    if result != "fixed":
        print(f"  ERROR: flat rate 24.5p expected 'fixed', got '{result}'")
        failed = True

    print("Test: Flat rate export")
    result = my_predbat.band_rate_text(15.0, export=True)
    if result != "fixed":
        print(f"  ERROR: flat export 15p expected 'fixed', got '{result}'")
        failed = True

    # --- Cosy Octopus (4p off-peak, 14.1p mid, 43.09p peak) ---
    print("Test: Cosy Octopus import rates")
    my_predbat.rate_min = 4.0
    my_predbat.rate_max = 43.09

    # 4p = bottom of range -> cheap (frac ~0.0)
    result = my_predbat.band_rate_text(4.0)
    if result != "cheap":
        print(f"  ERROR: Cosy 4p expected 'cheap', got '{result}'")
        failed = True

    # 14.1p -> frac = (14.1-4)/(43.09-4) = 10.1/39.09 = 0.258 -> cheap
    result = my_predbat.band_rate_text(14.1)
    if result != "cheap":
        print(f"  ERROR: Cosy 14.1p expected 'cheap', got '{result}'")
        failed = True

    # 28.7p -> frac = (28.7-4)/39.09 = 0.631 -> expensive
    result = my_predbat.band_rate_text(28.7)
    if result != "expensive":
        print(f"  ERROR: Cosy 28.7p expected 'expensive', got '{result}'")
        failed = True

    # 43.09p = top of range -> frac ~1.0 -> very expensive
    result = my_predbat.band_rate_text(43.09)
    if result != "very expensive":
        print(f"  ERROR: Cosy 43.09p expected 'very expensive', got '{result}'")
        failed = True

    # --- Flux (import: 17.09p off-peak, 28.76p day, 40.43p peak) ---
    print("Test: Flux import rates")
    my_predbat.rate_min = 17.09
    my_predbat.rate_max = 40.43

    # 17.09p -> frac 0.0 -> cheap
    result = my_predbat.band_rate_text(17.09)
    if result != "cheap":
        print(f"  ERROR: Flux 17.09p expected 'cheap', got '{result}'")
        failed = True

    # 28.76p -> frac = (28.76-17.09)/(40.43-17.09) = 11.67/23.34 = 0.5 -> expensive
    result = my_predbat.band_rate_text(28.76)
    if result != "expensive":
        print(f"  ERROR: Flux 28.76p expected 'expensive', got '{result}'")
        failed = True

    # 40.43p -> frac ~1.0 -> very expensive
    result = my_predbat.band_rate_text(40.43)
    if result != "very expensive":
        print(f"  ERROR: Flux 40.43p expected 'very expensive', got '{result}'")
        failed = True

    # --- Flux export (3.76p off-peak, 14.43p day, 25.1p peak) ---
    print("Test: Flux export rates")
    my_predbat.rate_export_min = 3.76
    my_predbat.rate_export_max = 25.1

    # 3.76p -> frac 0.0 -> very low
    result = my_predbat.band_rate_text(3.76, export=True)
    if result != "very low":
        print(f"  ERROR: Flux export 3.76p expected 'very low', got '{result}'")
        failed = True

    # 9p -> frac = (9-3.76)/(25.1-3.76) = 5.24/21.34 = 0.246 -> very low
    result = my_predbat.band_rate_text(9.0, export=True)
    if result != "very low":
        print(f"  ERROR: Flux export 9p expected 'very low', got '{result}'")
        failed = True

    # 14.43p -> frac = (14.43-3.76)/(25.1-3.76) = 10.67/21.34 = 0.5 -> low
    result = my_predbat.band_rate_text(14.43, export=True)
    if result != "low":
        print(f"  ERROR: Flux export 14.43p expected 'low', got '{result}'")
        failed = True

    # 19p -> frac = (19-3.76)/21.34 = 0.714 -> good
    result = my_predbat.band_rate_text(19.0, export=True)
    if result != "good":
        print(f"  ERROR: Flux export 19p expected 'good', got '{result}'")
        failed = True

    # 25.1p -> frac ~1.0 -> very good
    result = my_predbat.band_rate_text(25.1, export=True)
    if result != "very good":
        print(f"  ERROR: Flux export 25.1p expected 'very good', got '{result}'")
        failed = True

    # --- Edge cases ---
    print("Test: Edge cases")

    # Free import
    my_predbat.rate_min = 0.0
    my_predbat.rate_max = 30.0
    result = my_predbat.band_rate_text(0.0)
    if result != "free":
        print(f"  ERROR: 0p import expected 'free', got '{result}'")
        failed = True

    # Negative import (plunge pricing)
    result = my_predbat.band_rate_text(-5.0)
    if result != "negative":
        print(f"  ERROR: -5p import expected 'negative', got '{result}'")
        failed = True

    # Zero export
    my_predbat.rate_export_min = 0.0
    my_predbat.rate_export_max = 15.0
    result = my_predbat.band_rate_text(0.0, export=True)
    if result != "zero":
        print(f"  ERROR: 0p export expected 'zero', got '{result}'")
        failed = True

    # Negative export
    result = my_predbat.band_rate_text(-2.0, export=True)
    if result != "negative":
        print(f"  ERROR: -2p export expected 'negative', got '{result}'")
        failed = True

    print("**** band_rate_text tests completed ****")
    return failed
