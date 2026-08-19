# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for fast-mode month interpolation."""

import calendar

from annual_interpolate import ANCHOR_MONTHS, BASIS_LINEAR, BASIS_SOLAR_AFFINE, FAST_MODE_MAX_RATE_CV, build_interpolated_rows, choose_basis, rate_variability

YEAR = 2025
FIELDS = ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh", "battery_cycles"]


def days_in(month):
    """Return the number of days in the given month of the test year."""
    return calendar.monthrange(YEAR, month)[1]


def linear_pv():
    """Return a monthly PV curve that rises to midsummer and falls back, in kWh."""
    return {month: 100.0 + 500.0 * (1 - abs(month - 7) / 6.0) for month in range(1, 13)}


def anchor_rows_from(value_for):
    """Build anchor month rows whose every scenario field is value_for(month)."""
    rows = {}
    for month in ANCHOR_MONTHS:
        scenarios = {key: {field: value_for(month) for field in FIELDS} for key in ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]}
        rows[month] = {"month": month, "status": "ok", "days": days_in(month), "scenarios": scenarios}
    return rows


def test_annual_interpolate(my_predbat):
    """Interpolation reproduces a known affine curve, clamps, and falls back safely."""
    failed = False
    print("**** Testing annual fast-mode interpolation ****")
    pv = linear_pv()

    print("Test: an exact affine relationship is reproduced exactly")

    # per-day value = 2 + 0.5 * pv_per_day, so a correct least-squares fit through the four
    # anchors must recover every other month to floating point precision.
    def affine(month):
        """Monthly total whose per-day value is exactly 2 + 0.5 * pv per day."""
        return (2.0 + 0.5 * (pv[month] / days_in(month))) * days_in(month)

    rows = build_interpolated_rows(anchor_rows_from(affine), YEAR, pv)
    if sorted(rows) != [m for m in range(1, 13) if m not in ANCHOR_MONTHS]:
        print("  ERROR: expected the eight non-anchor months, got {}".format(sorted(rows)))
        failed = True
    for month, row in rows.items():
        got = row["scenarios"]["with_predbat"]["cost_p"]
        if abs(got - affine(month)) > 1e-3:
            print("  ERROR: month {} should reconstruct to {:.4f}, got {:.4f}".format(month, affine(month), got))
            failed = True

    print("Test: interpolated rows are marked and carry their provenance")
    row = rows[5]
    if row["status"] != "interpolated":
        print("  ERROR: status should be 'interpolated', got {!r}".format(row["status"]))
        failed = True
    if row["interpolated_from"] != {"anchors": list(ANCHOR_MONTHS), "basis": BASIS_SOLAR_AFFINE}:
        print("  ERROR: unexpected provenance {!r}".format(row["interpolated_from"]))
        failed = True
    if "sampled_days" in row:
        print("  ERROR: an interpolated month must not claim sampled days")
        failed = True
    if row["days"] != days_in(5):
        print("  ERROR: days should be the real month length, got {}".format(row["days"]))
        failed = True

    print("Test: a constant per-day value scales with month length, not flat totals")
    # Guards the per-day working space: February must come out smaller than March purely
    # because it is shorter, which a fit on raw monthly totals would smear away.
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 10.0 * days_in(month)), YEAR, pv)
    for month, row in rows.items():
        expected = 10.0 * days_in(month)
        if abs(row["scenarios"]["no_pvbat"]["cost_p"] - expected) > 1e-3:
            print("  ERROR: month {} constant per-day should total {:.2f}, got {:.2f}".format(month, expected, row["scenarios"]["no_pvbat"]["cost_p"]))
            failed = True

    print("Test: physical fields clamp at zero but cost may stay negative")

    # A steep slope through the anchors extrapolates below zero in the dark months; export
    # kWh cannot be negative, but an export-credit-dominated cost legitimately can be.
    def steep(month):
        """Monthly total that extrapolates well below zero in midwinter."""
        return (-40.0 + 0.9 * (pv[month] / days_in(month))) * days_in(month)

    rows = build_interpolated_rows(anchor_rows_from(steep), YEAR, pv)
    if any(row["scenarios"]["with_predbat"]["export_kwh"] < 0 for row in rows.values()):
        print("  ERROR: export_kwh must be clamped at zero")
        failed = True
    if not any(row["scenarios"]["with_predbat"]["cost_p"] < 0 for row in rows.values()):
        print("  ERROR: cost_p must be allowed to go negative")
        failed = True

    print("Test: no solar falls back to the linear basis")
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0), YEAR, None)
    if rows[5]["interpolated_from"]["basis"] != BASIS_LINEAR:
        print("  ERROR: a run with no solar should use the linear basis, got {!r}".format(rows[5]["interpolated_from"]["basis"]))
        failed = True

    print("Test: a degenerate (flat) PV curve falls back rather than dividing by zero")
    # Flat *per day*, which is the real degenerate case - a constant monthly total is not
    # flat once divided by differing month lengths, and still carries a fittable signal.
    flat = {month: 10.0 * days_in(month) for month in range(1, 13)}
    if choose_basis(list(ANCHOR_MONTHS), flat, YEAR) != BASIS_LINEAR:
        print("  ERROR: flat PV across the anchors should select the linear basis")
        failed = True
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0 * days_in(month)), YEAR, flat)
    if len(rows) != 8:
        print("  ERROR: the fallback must still produce all eight months, got {}".format(len(rows)))
        failed = True

    print("Test: the linear basis wraps the turn of the year")
    # January and February sit between the December and March anchors going forward round
    # the circle; treating the anchor list as a straight line would extrapolate off the end.
    values = {3: 10.0 * days_in(3), 6: 20.0 * days_in(6), 9: 30.0 * days_in(9), 12: 40.0 * days_in(12)}
    rows = build_interpolated_rows(anchor_rows_from(lambda month: values[month]), YEAR, None)
    for month in (1, 2):
        per_day = rows[month]["scenarios"]["no_pvbat"]["cost_p"] / days_in(month)
        if not 10.0 < per_day < 40.0:
            print("  ERROR: month {} should interpolate between the Dec (40/day) and Mar (10/day) anchors, got {:.2f}/day".format(month, per_day))
            failed = True

    print("Test: only the requested months are produced")
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0), YEAR, pv, months=[2, 5])
    if sorted(rows) != [2, 5]:
        print("  ERROR: expected only months 2 and 5, got {}".format(sorted(rows)))
        failed = True

    print("Test: derived fields are not interpolated")
    # export_credit_p_estimate is recomputed by run() from the month's real export rate;
    # interpolating it here would be overwritten at best and inconsistent at worst.
    base = anchor_rows_from(lambda month: 100.0)
    for month in ANCHOR_MONTHS:
        for key in base[month]["scenarios"]:
            base[month]["scenarios"][key]["export_credit_p_estimate"] = 55.0
    rows = build_interpolated_rows(base, YEAR, pv)
    if "export_credit_p_estimate" in rows[5]["scenarios"]["with_predbat"]:
        print("  ERROR: export_credit_p_estimate must be left for run() to attach")
        failed = True

    return failed


def test_annual_fast_mode_assembly(my_predbat):
    """The pieces run() assembles: month selection, provenance, and the anchor fallback."""
    failed = False
    print("**** Testing annual fast-mode assembly ****")
    pv = linear_pv()

    print("Test: fast mode plans only the anchor months")
    if sorted(ANCHOR_MONTHS) != [3, 6, 9, 12]:
        print("  ERROR: anchors should be Mar/Jun/Sep/Dec, got {}".format(sorted(ANCHOR_MONTHS)))
        failed = True

    print("Test: an unavailable month is not interpolated over")
    # A month with no rate data must stay unavailable, not be quietly fabricated - which is
    # why run() passes an explicit month list rather than letting the module fill everything.
    wanted = [month for month in range(1, 13) if month not in ANCHOR_MONTHS and month != 2]
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0 * days_in(month)), YEAR, pv, months=wanted)
    if 2 in rows:
        print("  ERROR: month 2 was excluded but got interpolated anyway")
        failed = True
    if len(rows) != 7:
        print("  ERROR: expected 7 interpolated months, got {}".format(len(rows)))
        failed = True

    print("Test: a banded tariff reads as stable and a volatile one does not")
    # Measured on the reference runs: Cosy 0.005, Agile 0.21. The threshold sits between,
    # far enough from both that neither is a borderline call.
    banded = {month: [22.5, 22.5, 22.5, 22.6, 22.5] for month in range(1, 13)}
    if rate_variability(banded) > FAST_MODE_MAX_RATE_CV:
        print("  ERROR: a fixed-band tariff should read as stable, got {:.3f}".format(rate_variability(banded)))
        failed = True
    volatile = {month: [8.0, 31.0, 15.0, 44.0, 21.0] for month in range(1, 13)}
    if rate_variability(volatile) <= FAST_MODE_MAX_RATE_CV:
        print("  ERROR: a day-to-day volatile tariff should exceed the limit, got {:.3f}".format(rate_variability(volatile)))
        failed = True

    print("Test: too little rate data reads as stable rather than blocking fast mode")
    if rate_variability({}) != 0.0:
        print("  ERROR: no rate data should return 0.0, got {!r}".format(rate_variability({})))
        failed = True
    if rate_variability({1: [20.0, 30.0]}) != 0.0:
        print("  ERROR: a month with under three sampled days should be skipped, got {!r}".format(rate_variability({1: [20.0, 30.0]})))
        failed = True

    print("Test: fewer than two surviving anchors produces nothing")
    single = {3: anchor_rows_from(lambda month: 100.0)[3]}
    if build_interpolated_rows(single, YEAR, pv) != {}:
        print("  ERROR: a single anchor must not be fitted - run() falls back to a full run instead")
        failed = True

    print("Test: two surviving anchors still work")
    two = {month: anchor_rows_from(lambda m: 100.0 * days_in(m))[month] for month in (3, 9)}
    rows = build_interpolated_rows(two, YEAR, pv)
    if len(rows) != 10:
        print("  ERROR: two anchors should fill the other ten months, got {}".format(len(rows)))
        failed = True
    if rows[5]["interpolated_from"]["anchors"] != [3, 9]:
        print("  ERROR: provenance should record the surviving anchors, got {!r}".format(rows[5]["interpolated_from"]["anchors"]))
        failed = True

    return failed
