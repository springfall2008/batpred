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

from const import CLOUD_FACTOR_PV10, PREDICT_STEP, CLOUD_ARRAY_MARGIN


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
    failed |= test_cloud_envelope_conserves_each_window(my_predbat)
    failed |= test_cloud_envelope_antiphase(my_predbat)
    failed |= test_cloud_duty_follows_band_asymmetry(my_predbat)
    failed |= test_pv90_ceiling_capped_by_array_size(my_predbat)
    failed |= test_pv_array_kwp_resolution(my_predbat)
    return failed


CLOUD_WINDOW = 30


def _ramped_series(my_predbat, peak=1.0):
    """A smooth day-shaped series over the horizon, so windows differ from one another."""
    minutes_now = my_predbat.minutes_now
    horizon = my_predbat.forecast_minutes + 2 * PREDICT_STEP
    item = {}
    for offset in range(horizon):
        minute = minutes_now + offset
        phase = ((minute % (24 * 60)) - 12 * 60) / (6.0 * 60)
        item[minute] = max(peak * (1.0 - phase * phase), 0.0) / 60.0
    return item


def test_cloud_envelope_conserves_each_window(my_predbat):
    """The envelope model holds the total over every complete 30-minute window.

    The pairwise borrow/repay model only conserves across adjacent step pairs, which caps how far a
    trough can fall - a step can never give back more than it holds. Conserving over a 30-minute
    window instead lets the duty cycle be uneven (four steps up, two down), which is what allows
    peaks to reach the p90 ceiling while troughs still fall far enough to matter.
    """
    failed = False
    print("**** Testing envelope-driven cloud modulation ****")

    minutes_now = my_predbat.minutes_now
    base_item = _ramped_series(my_predbat)
    ceiling_item = {minute: value * 1.4 for minute, value in base_item.items()}

    baseline = _forward_series(my_predbat, base_item)
    modulated = my_predbat.step_data_history(
        base_item, minutes_now, forward=True, cloud_ceiling=ceiling_item, cloud_duty=(4, 2)
    )

    print("Test: the envelope model modulates the series")
    if modulated == baseline:
        print("  ERROR: cloud_ceiling made no difference - the envelope model is inert")
        return True

    print("Test: every complete 30-minute window keeps its total")
    worst = 0.0
    for window_start in range(0, my_predbat.forecast_minutes - CLOUD_WINDOW, CLOUD_WINDOW):
        offsets = [window_start + n for n in range(0, CLOUD_WINDOW, PREDICT_STEP)]
        if any(offset not in baseline for offset in offsets):
            continue
        before = sum(baseline[offset] for offset in offsets)
        after = sum(modulated[offset] for offset in offsets)
        worst = max(worst, abs(after - before))
    if worst > 1e-6:
        print("  ERROR: worst 30-minute window drifted by {:.6f} kWh - the model must conserve within each window".format(worst))
        failed = True

    print("Test: no step is raised above its ceiling")
    ceiling_step = _forward_series(my_predbat, ceiling_item)
    over = [offset for offset in modulated if modulated[offset] > ceiling_step.get(offset, 0.0) + 1e-6]
    if over:
        print("  ERROR: {} step(s) exceed the ceiling, worst {:.6f} over".format(len(over), max(modulated[o] - ceiling_step[o] for o in over)))
        failed = True

    print("Test: no step is driven below zero")
    negative = [offset for offset in modulated if modulated[offset] < -1e-9]
    if negative:
        print("  ERROR: {} step(s) went negative, worst {:.6f}".format(len(negative), min(modulated[o] for o in negative)))
        failed = True

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


def test_cloud_envelope_antiphase(my_predbat):
    """A flipped series lowers exactly where the plain one raises.

    This is what flip has always been for - covering both directions within the same 5 minutes so
    the planner cannot assume every scenario dips at once. The uneven duty cycle keeps that
    property only if the flipped series takes the complementary duty: where p50 spends four steps
    up and two down, p10 spends two up and four down, and the phase offset lands its two up-steps
    on p50's two down-steps.
    """
    failed = False
    print("**** Testing envelope antiphase between the p50 and p10 series ****")

    minutes_now = my_predbat.minutes_now
    p50_item = _ramped_series(my_predbat)
    p90_item = {minute: value * 1.4 for minute, value in p50_item.items()}
    p10_item = {minute: value * 0.5 for minute, value in p50_item.items()}

    p50_base = _forward_series(my_predbat, p50_item)
    p10_base = _forward_series(my_predbat, p10_item)

    p50 = my_predbat.step_data_history(p50_item, minutes_now, forward=True, cloud_ceiling=p90_item, cloud_duty=(4, 2))
    p10 = my_predbat.step_data_history(p10_item, minutes_now, forward=True, cloud_ceiling=p50_item, cloud_duty=(2, 4), flip=True)

    raised_p50 = {offset for offset in p50_base if p50[offset] > p50_base[offset] + 1e-9}
    lowered_p10 = {offset for offset in p10_base if p10[offset] < p10_base[offset] - 1e-9}
    raised_p10 = {offset for offset in p10_base if p10[offset] > p10_base[offset] + 1e-9}

    print("Test: both series actually move")
    if not raised_p50 or not raised_p10:
        print("  ERROR: expected both series to raise some steps, got {} p50 and {} p10".format(len(raised_p50), len(raised_p10)))
        return True

    print("Test: no step is raised in both series at once")
    both = raised_p50 & raised_p10
    if both:
        print("  ERROR: {} step(s) are raised in both series - the scenarios peak together instead of covering both directions".format(len(both)))
        failed = True

    print("Test: every step p50 raises is one p10 lowers")
    missed = raised_p50 - lowered_p10
    if missed:
        print("  ERROR: {} of {} step(s) raised in p50 are not lowered in p10".format(len(missed), len(raised_p50)))
        failed = True

    return failed


def test_cloud_duty_follows_band_asymmetry(my_predbat):
    """The duty cycle comes from the band's own shape, not a fixed number.

    Peaks reach the p90 ceiling and troughs reach the p10 floor at the same time only when the
    up:down step ratio matches (p50-p10):(p90-p50). Solcast's band is asymmetric - the downside is
    typically about twice the upside - so a fixed 1:1 alternation always under-reaches on one side.
    """
    failed = False
    print("**** Testing cloud duty cycle derivation ****")

    minutes_now = my_predbat.minutes_now
    horizon = my_predbat.forecast_minutes

    def band(down_gap, up_gap):
        p50 = {minutes_now + n: 1.0 for n in range(horizon)}
        p10 = {minutes_now + n: 1.0 - down_gap for n in range(horizon)}
        p90 = {minutes_now + n: 1.0 + up_gap for n in range(horizon)}
        return p50, p10, p90

    print("Test: a symmetric band gives an even duty cycle")
    duty = my_predbat.get_cloud_duty(minutes_now, *band(0.2, 0.2))
    if duty != (3, 3):
        print("  ERROR: symmetric band gave duty {}, expected (3, 3)".format(duty))
        failed = True

    print("Test: a band with twice the downside spends twice as many steps up")
    duty = my_predbat.get_cloud_duty(minutes_now, *band(0.4, 0.2))
    if duty != (4, 2):
        print("  ERROR: 2:1 band gave duty {}, expected (4, 2)".format(duty))
        failed = True

    print("Test: no upside band means no envelope model")
    # A forecast source with no p90 falls back to a copy of the p50, leaving nothing to reach for.
    # Returning None lets the caller keep the legacy proportional model rather than silently
    # switching the cloud model off for those users.
    duty = my_predbat.get_cloud_duty(minutes_now, *band(0.4, 0.0))
    if duty is not None:
        print("  ERROR: a flat p90 gave duty {}, expected None so the caller falls back".format(duty))
        failed = True

    return failed


def test_pv90_ceiling_capped_by_array_size(my_predbat):
    """p90's own ceiling is an extrapolation, and needs a physical rail.

    p50 reaches for p90 and p10 reaches for p50, but p90 is the top percentile the forecaster
    publishes - there is no next one up. Extending the band by its own width is the natural
    continuation, but on a very uncertain day that can extrapolate past what the array could ever
    produce, so it is capped at the DC array size plus a margin for cloud-edge enhancement.
    """
    failed = False
    print("**** Testing the p90 modulation ceiling ****")

    minutes_now = my_predbat.minutes_now
    horizon = my_predbat.forecast_minutes
    p50 = {minutes_now + n: 0.010 for n in range(horizon)}
    p90 = {minutes_now + n: 0.015 for n in range(horizon)}

    saved = getattr(my_predbat, "pv_array_kwp", 0)
    try:
        print("Test: with no array size known, p90 extends by its own band width")
        my_predbat.pv_array_kwp = 0
        ceiling = my_predbat.get_pv90_cloud_ceiling(minutes_now, p50, p90)
        expected = 0.015 + (0.015 - 0.010)
        if abs(ceiling.get(minutes_now, 0.0) - expected) > 1e-9:
            print("  ERROR: uncapped ceiling was {}, expected {}".format(ceiling.get(minutes_now), expected))
            failed = True

        print("Test: the array size caps the ceiling")
        # 0.9 kWp -> 0.9 * 1.2 / 60 = 0.018 kWh per minute, above p90 but below the extrapolation
        my_predbat.pv_array_kwp = 0.9
        capped = my_predbat.get_pv90_cloud_ceiling(minutes_now, p50, p90)
        limit = 0.9 * CLOUD_ARRAY_MARGIN / 60.0
        worst = max(capped.values())
        if worst > limit + 1e-9:
            print("  ERROR: ceiling reached {} but the array cap is {}".format(worst, limit))
            failed = True
        if abs(worst - limit) > 1e-9:
            print("  ERROR: ceiling was {} but should have been held at the cap {}".format(worst, limit))
            failed = True

        print("Test: a cap below p90 never drags the ceiling under p90 itself")
        # A ceiling below the series it modulates would invert the scenario - p90's upside case
        # would start life as a downside one.
        my_predbat.pv_array_kwp = 0.1
        tiny = my_predbat.get_pv90_cloud_ceiling(minutes_now, p50, p90)
        if tiny.get(minutes_now, 0.0) < 0.015 - 1e-9:
            print("  ERROR: ceiling {} fell below p90 {}".format(tiny.get(minutes_now), 0.015))
            failed = True
    finally:
        my_predbat.pv_array_kwp = saved

    return failed


def test_pv_array_kwp_resolution(my_predbat):
    """The DC array size comes from the forecast provider unless apps.yaml overrides it.

    Forecast Solar and Open-Meteo already declare kwp per array, and fetch_pv_forecast already
    totals it, so the common case needs no configuration at all. Solcast and the HA integrations
    publish no array size (the total arrives as the 9999 "unknown" sentinel), which is the case the
    apps.yaml override exists for.
    """
    failed = False
    print("**** Testing DC array size resolution ****")

    saved_args = dict(my_predbat.args)
    saved_kwp = getattr(my_predbat, "pv_array_kwp", 0)
    try:
        print("Test: a detected array size is used when apps.yaml says nothing")
        my_predbat.args.pop("pv_array_kwp", None)
        my_predbat.resolve_pv_array_kwp(17.6)
        if abs(my_predbat.pv_array_kwp - 17.6) > 1e-9:
            print("  ERROR: detected 17.6 kWp but resolved to {}".format(my_predbat.pv_array_kwp))
            failed = True

        print("Test: an apps.yaml setting overrides what the provider declared")
        # Declared figures are routinely understated - users enter the inverter size, or one string
        # of two - so an explicit setting has to win.
        my_predbat.args["pv_array_kwp"] = 20.0
        my_predbat.resolve_pv_array_kwp(17.6)
        if abs(my_predbat.pv_array_kwp - 20.0) > 1e-9:
            print("  ERROR: apps.yaml said 20.0 but resolved to {}".format(my_predbat.pv_array_kwp))
            failed = True

        print("Test: the unknown sentinel leaves the cap inert")
        my_predbat.args.pop("pv_array_kwp", None)
        my_predbat.resolve_pv_array_kwp(9999)
        if my_predbat.pv_array_kwp:
            print("  ERROR: an unknown array size resolved to {} instead of leaving the cap off".format(my_predbat.pv_array_kwp))
            failed = True
    finally:
        my_predbat.args = saved_args
        my_predbat.pv_array_kwp = saved_kwp

    return failed
