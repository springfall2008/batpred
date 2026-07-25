# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction load profile data tables."""

from annual_profiles import DAY_BAND_SLOTS, HOURLY_SHAPE, MONTH_WEIGHTS, NIGHT_BAND_SLOTS, SHAPE_TILT_FRACTION, half_hour_shape


def test_annual_profiles(my_predbat):
    """Verify the profile tables are well formed and normalise correctly."""
    failed = False
    print("**** Testing annual_profiles ****")

    print("Test: HOURLY_SHAPE has 24 positive entries")
    if len(HOURLY_SHAPE) != 24:
        print("  ERROR: expected 24 hourly weights, got {}".format(len(HOURLY_SHAPE)))
        failed = True
    if any(value <= 0 for value in HOURLY_SHAPE):
        print("  ERROR: all hourly weights must be positive")
        failed = True

    print("Test: half_hour_shape returns 48 values summing to 1.0")
    shape = half_hour_shape()
    if len(shape) != 48:
        print("  ERROR: expected 48 half-hourly values, got {}".format(len(shape)))
        failed = True
    total = sum(shape)
    if abs(total - 1.0) > 1e-9:
        print("  ERROR: half_hour_shape must sum to 1.0, got {}".format(total))
        failed = True

    print("Test: the evening peak exceeds the overnight trough")
    evening = sum(shape[36:42])
    overnight = sum(shape[4:10])
    if evening <= overnight:
        print("  ERROR: evening 18:00-21:00 share {} should exceed overnight 02:00-05:00 share {}".format(evening, overnight))
        failed = True

    print("Test: MONTH_WEIGHTS has 12 positive entries with winter above summer")
    if len(MONTH_WEIGHTS) != 12:
        print("  ERROR: expected 12 month weights, got {}".format(len(MONTH_WEIGHTS)))
        failed = True
    if any(value <= 0 for value in MONTH_WEIGHTS):
        print("  ERROR: all month weights must be positive")
        failed = True
    if MONTH_WEIGHTS[0] <= MONTH_WEIGHTS[6]:
        print("  ERROR: January weight {} should exceed July weight {}".format(MONTH_WEIGHTS[0], MONTH_WEIGHTS[6]))
        failed = True

    print("Test: the night and day bands are disjoint and correctly sized")
    if NIGHT_BAND_SLOTS != list(range(0, 14)):
        print("  ERROR: NIGHT_BAND_SLOTS should cover 00:00-07:00, got {}".format(NIGHT_BAND_SLOTS))
        failed = True
    if DAY_BAND_SLOTS != list(range(14, 40)):
        print("  ERROR: DAY_BAND_SLOTS should cover 07:00-20:00, got {}".format(DAY_BAND_SLOTS))
        failed = True
    if set(NIGHT_BAND_SLOTS) & set(DAY_BAND_SLOTS):
        print("  ERROR: night and day bands must be disjoint")
        failed = True

    print("Test: SHAPE_TILT_FRACTION is a sane proportion")
    if not 0.0 < SHAPE_TILT_FRACTION < 0.5:
        print("  ERROR: SHAPE_TILT_FRACTION should be between 0 and 0.5, got {}".format(SHAPE_TILT_FRACTION))
        failed = True

    return failed
