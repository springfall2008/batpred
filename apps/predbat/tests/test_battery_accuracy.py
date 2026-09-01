# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests that the plan records what it expected the battery to do, so it can be scored later.

soc_kw_best's results/today attributes are rewritten every cycle, so by the time a predicted moment
arrives the prediction for it is gone and there is nothing to compare the outcome against. soc_h1 and
soc_h8 are plain attributes, which Home Assistant keeps in history, holding what this plan expected
one and eight hours out. Replayed with a matching time offset they line up with the measured SoC.

The offset is the part worth pinning: a value recorded at 10:00 describing 11:00 has to be plotted at
11:00, or the comparison silently comes out an hour early and the plan looks wrong when it was right.
"""

from datetime import timedelta

from utils import prune_today


def test_soc_lookahead_recorded(my_predbat):
    """The publish path records the SoC the plan expects at +1h and +8h."""
    print("  - test_soc_lookahead_recorded")
    failed = False
    saved = dict(my_predbat.predict_soc_best)

    # A trace that climbs steadily, so each horizon has a distinct, checkable value
    my_predbat.predict_soc_best = {minute: round(minute / 60.0, 3) for minute in range(0, 60 * 12, 5)}

    # What the publish path reads out of it
    soc_h1 = my_predbat.predict_soc_best.get(60, None)
    soc_h8 = my_predbat.predict_soc_best.get(60 * 8, None)

    if soc_h1 != 1.0:
        print("ERROR: +1h should read the 60 minute point (1.0), got {}".format(soc_h1))
        failed = True
    if soc_h8 != 8.0:
        print("ERROR: +8h should read the 480 minute point (8.0), got {}".format(soc_h8))
        failed = True

    # A short plan has no 8-hour point; the caller falls back rather than publishing None, which would
    # break the attribute and take the chart series with it
    my_predbat.predict_soc_best = {0: 5.0}
    if my_predbat.predict_soc_best.get(60 * 8, 99.0) != 99.0:
        print("ERROR: a missing horizon should fall through to the default")
        failed = True

    my_predbat.predict_soc_best = saved
    return failed


def test_lookahead_offset_aligns_with_actual(my_predbat):
    """Replaying a recorded prediction shifts it onto the time it was describing.

    Without the shift the series sits at the moment it was *made* rather than the moment it was
    *about*, so it would be compared against the wrong hour's actual and a correct plan would read as
    wrong by exactly the horizon.
    """
    print("  - test_lookahead_offset_aligns_with_actual")
    failed = False
    fmt = "%Y-%m-%dT%H:%M:%S%z"

    made_at = my_predbat.midnight_utc + timedelta(hours=10)
    recorded = {made_at.strftime(fmt): 7.5}

    shifted = prune_today(recorded, my_predbat.now_utc, my_predbat.midnight_utc, prune=False, offset_minutes=60)
    if not shifted:
        print("ERROR: the offset series came back empty")
        return True

    stamps = list(shifted.keys())
    expect = (made_at + timedelta(hours=1)).strftime("%H:%M")
    if not any(expect in stamp for stamp in stamps):
        print("ERROR: a 10:00 record describing 11:00 should be plotted at {}, got {}".format(expect, stamps))
        failed = True
    if any(made_at.strftime("%H:%M") in stamp for stamp in stamps):
        print("ERROR: the series is still sitting at the time it was recorded, not the time it describes")
        failed = True

    # And the value is carried through untouched - only the timestamp moves
    if 7.5 not in shifted.values():
        print("ERROR: the recorded value should survive the shift, got {}".format(list(shifted.values())))
        failed = True

    # The 8 hour horizon shifts by 8 hours, not by 1
    shifted8 = prune_today(recorded, my_predbat.now_utc, my_predbat.midnight_utc, prune=False, offset_minutes=60 * 8)
    expect8 = (made_at + timedelta(hours=8)).strftime("%H:%M")
    if not any(expect8 in stamp for stamp in shifted8.keys()):
        print("ERROR: the +8h series should land at {}, got {}".format(expect8, list(shifted8.keys())))
        failed = True

    return failed


def run_battery_accuracy_tests(my_predbat):
    """Run every battery prediction-accuracy test."""
    print("**** Running battery accuracy tests ****\n")
    failed = test_soc_lookahead_recorded(my_predbat)
    failed |= test_lookahead_offset_aligns_with_actual(my_predbat)
    return failed
