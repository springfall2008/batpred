# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Reconstruct the months a fast-mode run never planned, from the ones it did.

Pure functions only - no I/O, no Predbat import - so the curve can be unit tested against
known inputs and re-scored against stored reference runs without standing up an engine.

The curve is affine in solar: a month's per-day figure is modelled as ``a + b * pv_per_day``
and fitted by least squares over the anchor months, independently for every scenario and
every field. Fitting per field is the point - ``no_pvbat`` cost is load-driven and lands
near ``b = 0`` while ``export_kwh`` is steeply solar-driven, and one shared shape would
serve neither. Working in per-day space stops February's 28 days reading as a seasonal dip.

Measured against five twelve-month reference runs, this contributes under 1% error to the
annual savings figure; see docs/superpowers/specs/2026-08-18-annual-fast-mode-design.md.
"""

import calendar

# One per season, spanning midwinter to midsummer so the fit can resolve the solar slope.
# March/June/September/December measurably beat the alternatives on the savings figures a
# reader actually acts on: on the Cosy reference this set reconstructs them to -0.5% and
# +0.2%, where (1, 4, 7, 10) gives -10.6% on the Predbat saving.
ANCHOR_MONTHS = (3, 6, 9, 12)

# Above this coefficient of variation in daily average import price, a month's economics are
# no longer a smooth function of solar and a sampled month stops representing its neighbours,
# so fast mode declines and the caller plans the year in full instead. Measured across the
# reference runs: Agile 0.21 (max month 0.33) against 0.005 for Cosy (max 0.039). Banded
# tariffs sit near zero by construction - their bands are identical every day of the month -
# so the gap either side of this threshold is roughly fortyfold, not marginal.
FAST_MODE_MAX_RATE_CV = 0.10

BASIS_SOLAR_AFFINE = "solar_affine"
BASIS_LINEAR = "linear"
DEFAULT_BASIS = BASIS_SOLAR_AFFINE

# Cost is the one field that may legitimately be negative (export credit exceeding import
# spend). Everything else is a physical quantity that an extrapolated fit could otherwise
# drive below zero - negative December export being the obvious way to get it wrong.
SIGNED_FIELDS = ("cost_p",)

# Recomputed by run() from the month's real average export rate, so interpolating it here
# would be overwritten at best and silently inconsistent at worst.
DERIVED_FIELDS = ("export_credit_p_estimate",)

# Below this variance in per-day anchor PV there is no solar signal to fit against, and the
# least-squares denominator is not safely invertible.
_MIN_PV_VARIANCE = 1e-9


def _fit_affine(xs, ys):
    """Return least-squares ``(intercept, slope)`` for ``y = a + b * x``, or None if degenerate."""
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator < _MIN_PV_VARIANCE:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    return mean_y - slope * mean_x, slope


def _cyclic_linear(anchors, values, month):
    """Interpolate linearly around the month circle, wrapping December to January.

    The year is a cycle, so January sits between the December and March anchors going
    forward rather than off the end of a straight list.
    """
    count = len(anchors)
    for index in range(count):
        start = anchors[index]
        end = anchors[(index + 1) % count]
        span = (end - start) % 12 or 12
        offset = (month - start) % 12
        if offset <= span:
            fraction = offset / float(span)
            return values[index] * (1 - fraction) + values[(index + 1) % count] * fraction
    return values[0]


def rate_variability(daily_means_by_month):
    """Return the mean within-month coefficient of variation of daily average import price.

    Fast mode reconstructs unplanned months from planned ones, which only holds when a
    month's economics are a smooth function of solar. On a tariff whose whole price level
    moves day to day rather than following a fixed daily pattern, they are not: a couple of
    sampled days stop representing their own month, let alone its neighbours, and the
    reconstructed savings can be tens of percent out. This is the cheap signal that tells
    the two cases apart before any of that error is reported as a payback figure.

    Returns 0.0 when there is nothing measurable, which reads as "stable" - the caller then
    proceeds with fast mode, matching the behaviour for a tariff with no rate data to judge.
    """
    ratios = []
    for values in daily_means_by_month.values():
        # Two days cannot describe a month's spread; a month that short is skipped rather
        # than contributing a wildly over- or under-stated figure to the mean.
        if len(values) < 3:
            continue
        mean_value = sum(values) / len(values)
        if abs(mean_value) < 1e-9:
            continue
        variance = sum((value - mean_value) ** 2 for value in values) / len(values)
        ratios.append((variance**0.5) / abs(mean_value))
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios)


def choose_basis(anchor_months, monthly_pv, year):
    """Return the basis to use for this run: solar-affine when there is a solar signal to fit.

    Chosen once for the whole run rather than per field, so every interpolated row reports
    one honest basis in its provenance block.
    """
    if not monthly_pv:
        return BASIS_LINEAR
    per_day = [monthly_pv.get(month, 0.0) / calendar.monthrange(year, month)[1] for month in anchor_months]
    mean_pv = sum(per_day) / len(per_day)
    if sum((value - mean_pv) ** 2 for value in per_day) < _MIN_PV_VARIANCE:
        return BASIS_LINEAR
    return BASIS_SOLAR_AFFINE


def build_interpolated_rows(anchor_rows, year, monthly_pv, months=None, basis=None):
    """Return ``{month: row}`` for every wanted month absent from ``anchor_rows``.

    ``anchor_rows`` maps month number to a planned month row (as ``run()`` builds it, with a
    ``scenarios`` dict). ``monthly_pv`` maps month number to that month's total actual PV
    kWh, or is None for a battery-only run. ``months`` defaults to every month of the year
    that is not an anchor; pass a list to skip months already known to be unavailable.

    The returned rows carry no ``standing_charge_p`` and no ``export_credit_p_estimate``:
    both need tariff data, so ``run()`` attaches them.
    """
    anchors = sorted(anchor_rows)
    if len(anchors) < 2:
        # One point cannot define a line. The caller is expected to have abandoned fast mode
        # before reaching here; returning nothing keeps that contract enforceable rather
        # than inventing a flat year from a single month.
        return {}

    if basis is None:
        basis = choose_basis(anchors, monthly_pv, year)
    if months is None:
        months = [month for month in range(1, 13) if month not in anchors]

    days = {month: calendar.monthrange(year, month)[1] for month in range(1, 13)}
    pv_per_day = {month: (monthly_pv.get(month, 0.0) / days[month] if monthly_pv else 0.0) for month in range(1, 13)}
    anchor_pv = [pv_per_day[month] for month in anchors]

    scenario_keys = list(anchor_rows[anchors[0]]["scenarios"].keys())
    provenance = {"anchors": anchors, "basis": basis}

    rows = {}
    for month in months:
        rows[month] = {
            "month": month,
            "status": "interpolated",
            "days": days[month],
            "scenarios": {key: {} for key in scenario_keys},
            "interpolated_from": dict(provenance),
        }

    for key in scenario_keys:
        fields = [field for field in anchor_rows[anchors[0]]["scenarios"][key] if field not in DERIVED_FIELDS]
        for field in fields:
            per_day = [anchor_rows[month]["scenarios"][key][field] / days[month] for month in anchors]
            fit = _fit_affine(anchor_pv, per_day) if basis == BASIS_SOLAR_AFFINE else None
            for month in months:
                if fit is not None:
                    value = (fit[0] + fit[1] * pv_per_day[month]) * days[month]
                else:
                    value = _cyclic_linear(anchors, per_day, month) * days[month]
                if field not in SIGNED_FIELDS:
                    value = max(0.0, value)
                rows[month]["scenarios"][key][field] = round(value, 3)

    return rows
