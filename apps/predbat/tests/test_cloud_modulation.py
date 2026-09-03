# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for step_data_history()'s divergence model - metric_cloud_enable and load divergence.

The model raises and lowers alternate 5-minute steps while keeping the total, so the planner stops
assuming PV exactly blankets load. It is selected on per-array: pv_forecast_minute_step and every
load array take it plain, pv_forecast_minute10_step takes it with flip=True so the 10% scenario
diverges in antiphase.

Nothing previously asserted that either knob had an effect, so the whole suite passed while the
model was inert for every caller but the PV10 one - see step_data_history() in fetch.py for why.
"""

from const import CLOUD_FACTOR_PV10, PREDICT_STEP


def _flat_forward_series(my_predbat, cloud_factor=None, flip=False, value=1.0):
    """Run step_data_history() forward over a flat series, returning the stepped values."""
    minutes_now = my_predbat.minutes_now
    item = {minute: value for minute in range(minutes_now, minutes_now + my_predbat.forecast_minutes + 2 * PREDICT_STEP)}
    return my_predbat.step_data_history(item, minutes_now, forward=True, cloud_factor=cloud_factor, flip=flip)


def run_cloud_modulation_tests(my_predbat):
    """Run every divergence-model test."""
    failed = False
    failed |= test_cloud_modulation(my_predbat)
    failed |= test_cloud_modulation_non_flat(my_predbat)
    return failed


def _forward_series(my_predbat, item, cloud_factor=None, flip=False, load_baseline={}):
    """Run step_data_history() forward over a caller-supplied series."""
    return my_predbat.step_data_history(item, my_predbat.minutes_now, forward=True, cloud_factor=cloud_factor, flip=flip, load_baseline=load_baseline)


def test_cloud_modulation_non_flat(my_predbat):
    """The properties that matter on a series the model can actually distort.

    On a flat series the model is exactly net-zero by construction - an on-step borrows
    min(v, v_next) * cf and the next off-step repays all of it - so a flat fixture cannot see the
    behaviour that matters. A trough shallower than the borrow can only repay part of it, which is
    where the model stops conserving and where a load_baseline floor can be breached.
    """
    failed = False
    print("**** Testing cloud/load divergence on a non-flat series ****")

    minutes_now = my_predbat.minutes_now
    horizon = my_predbat.forecast_minutes + 2 * PREDICT_STEP

    # A high plateau with a narrow trough - the trough is far smaller than the borrow taken off the
    # step before it, so it cannot repay in full
    def value_at(minute):
        return 0.02 if (minute - minutes_now) % (4 * PREDICT_STEP) == PREDICT_STEP else 1.0

    item = {minute: value_at(minute) for minute in range(minutes_now, minutes_now + horizon)}
    baseline = _forward_series(my_predbat, item)
    modulated = _forward_series(my_predbat, item, cloud_factor=CLOUD_FACTOR_PV10)

    print("Test: modulation never inflates the total")
    total_before, total_after = sum(baseline.values()), sum(modulated.values())
    if total_after > total_before + 1e-6:
        print("  ERROR: total rose from {} to {} - the model may under-repay a borrow, but must never create energy".format(total_before, total_after))
        failed = True

    print("Test: no step is driven negative")
    negative = [minute for minute, value in modulated.items() if value < 0]
    if negative:
        print("  ERROR: {} step(s) went negative, first at minute {}".format(len(negative), negative[0]))
        failed = True

    print("Test: the dynamic load baseline floor survives the subtract")
    floor = 4.5
    floored = _forward_series(my_predbat, item, cloud_factor=CLOUD_FACTOR_PV10, load_baseline={minute: floor for minute in item})
    # Only where the floor was actually applied - the stepped series runs a plan interval past the
    # supplied data, and those tail steps legitimately have no baseline
    breached = [minute for minute, value in floored.items() if value < floor - 1e-6 and (minutes_now + minute) in item]
    if breached:
        print("  ERROR: {} step(s) fell below the load_baseline floor of {}, first at minute {} ({})".format(len(breached), floor, breached[0], floored[breached[0]]))
        failed = True

    return failed


def test_cloud_modulation(my_predbat):
    """Verify the divergence model actually modulates, on both the plain and flipped arrays."""
    failed = False
    print("**** Testing cloud/load divergence modulation ****")

    baseline = _flat_forward_series(my_predbat)
    plain = _flat_forward_series(my_predbat, cloud_factor=CLOUD_FACTOR_PV10)
    flipped = _flat_forward_series(my_predbat, cloud_factor=CLOUD_FACTOR_PV10, flip=True)

    print("Test: a cloud factor modulates the plain (non-flipped) series")
    # The regression: every caller that does not pass flip - the central PV forecast and every load
    # array - was left completely unmodulated.
    if plain == baseline:
        print("  ERROR: cloud_factor made no difference without flip - the divergence model is inert for the central PV and load forecasts")
        failed = True

    print("Test: a cloud factor modulates the flipped series")
    if flipped == baseline:
        print("  ERROR: cloud_factor made no difference with flip set")
        failed = True

    print("Test: flip puts the flipped series in antiphase with the plain one")
    # Every step the plain series raises, the flipped one must lower, and vice versa - that is the
    # entire purpose of flip, and it is meaningless unless the plain series is modulated too.
    raised_plain = {minute for minute in baseline if plain[minute] > baseline[minute]}
    raised_flipped = {minute for minute in baseline if flipped[minute] > baseline[minute]}
    if not raised_plain or not raised_flipped:
        print("  ERROR: expected both series to raise some steps, got {} plain and {} flipped".format(len(raised_plain), len(raised_flipped)))
        failed = True
    elif raised_plain & raised_flipped:
        print("  ERROR: {} step(s) are raised in both series - flip should invert the phase, not repeat it".format(len(raised_plain & raised_flipped)))
        failed = True

    print("Test: modulation alternates step by step rather than running in blocks")
    # Leading steps can be untouched: the model only subtracts what a previous step added, so a
    # series starting on a lowering step has nothing to give back yet. From the first step that
    # actually moves, raises and lowers must strictly alternate.
    moves = [("R" if plain[minute] > baseline[minute] else "L") for minute in sorted(baseline) if plain[minute] != baseline[minute]]
    if len(moves) < 4:
        print("  ERROR: expected the model to move most steps, only {} moved".format(len(moves)))
        failed = True
    elif any(first == second for first, second in zip(moves, moves[1:])):
        print("  ERROR: raises and lowers do not alternate ({}...) - the model is meant to move every other 5-minute step".format("".join(moves[:8])))
        failed = True

    print("Test: the modulation roughly preserves the total")
    # "keeps the same total" - what is added on a raised step is taken off the next one, so the sum
    # can only drift by the tail of the series, not by a proportion of it.
    for name, series in (("plain", plain), ("flipped", flipped)):
        total_before = sum(baseline.values())
        total_after = sum(series.values())
        if total_before and abs(total_after - total_before) / total_before > 0.01:
            print("  ERROR: {} series total moved from {} to {} - the model should redistribute, not scale".format(name, total_before, total_after))
            failed = True

    return failed
