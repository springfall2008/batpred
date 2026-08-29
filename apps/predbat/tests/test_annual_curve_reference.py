# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Guards fast mode's two measured claims against stored twelve-month reference runs.

The claims are that the volatility guard sorts tariffs correctly, and that on a tariff it
lets through, four anchor months reconstruct the savings a reader acts on. Both were
established by running real twelve-month projections; this keeps a later change from
quietly undoing either. Skips when no fixture is present, so a checkout without them stays
green rather than failing for a missing file.
"""

import json
import os

from annual_interpolate import ANCHOR_MONTHS, BASIS_LINEAR, BASIS_SOLAR_AFFINE, FAST_MODE_MAX_RATE_CV, build_interpolated_rows, rate_variability

# Agile is the tariff fast mode must decline; Cosy is the one it must accept and get right.
FIXTURES = {"annual_reference_agile.json": False, "annual_reference_cosy.json": True}


def fixture_path(name):
    """Return the path to a reference fixture, or None when it is not present."""
    for base in ("cases", os.path.join("coverage", "cases")):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    return None


def savings(rows):
    """Return the two headline savings figures, which is what payback is built from."""

    def total(key):
        """Annual cost in pence for one scenario."""
        return sum(rows[month]["scenarios"][key]["cost_p"] for month in rows)

    return total("no_pvbat") - total("without_predbat"), total("without_predbat") - total("with_predbat")


def rebuild(doc, basis):
    """Reconstruct the whole year from the anchor months and return its savings."""
    months = {entry["month"]: entry for entry in doc["months"]}
    monthly_pv = {int(month): value for month, value in doc["monthly_pv"].items()}
    anchors = {month: months[month] for month in ANCHOR_MONTHS if month in months}
    rebuilt = build_interpolated_rows(anchors, doc["year"], monthly_pv, basis=basis)
    return savings({**anchors, **rebuilt})


def test_annual_curve_reference(my_predbat):
    """The volatility guard sorts the reference tariffs, and the curve holds on the one it passes."""
    failed = False
    print("**** Testing annual fast-mode curve against reference runs ****")
    seen = False

    for name, should_pass in FIXTURES.items():
        path = fixture_path(name)
        if not path:
            continue
        seen = True
        doc = json.load(open(path))
        label = name.replace("annual_reference_", "").replace(".json", "")

        variability = rate_variability({int(month): values for month, values in doc["daily_mean_import_rates"].items()})
        accepted = variability <= FAST_MODE_MAX_RATE_CV
        print("  {}: rate variability {:.3f} (limit {:.2f}) -> fast mode {}".format(label, variability, FAST_MODE_MAX_RATE_CV, "accepted" if accepted else "declined"))
        if accepted != should_pass:
            print("  ERROR: {} should have been {} by the volatility guard, variability {:.3f} against limit {:.2f}".format(label, "accepted" if should_pass else "declined", variability, FAST_MODE_MAX_RATE_CV))
            failed = True

        if not should_pass:
            # A declined tariff never reaches interpolation, so its curve accuracy is not a
            # claim this feature makes. The guard verdict above is the whole test for it.
            continue

        true_system, true_predbat = savings({entry["month"]: entry for entry in doc["months"]})
        system, predbat = rebuild(doc, BASIS_SOLAR_AFFINE)
        system_error = abs(system - true_system) / abs(true_system) * 100
        predbat_error = abs(predbat - true_predbat) / abs(true_predbat) * 100
        print("  {}: solar_affine savings error - system {:.2f}%, predbat {:.2f}%".format(label, system_error, predbat_error))
        # Measured at 0.5% and 0.2% when the basis was chosen. 5% is slack for rounding and
        # for a regenerated fixture, while still catching a basis that has actually broken.
        if system_error > 5.0 or predbat_error > 5.0:
            print("  ERROR: {} - fast mode must reconstruct the savings within 5%, got system {:.2f}% and predbat {:.2f}%".format(label, system_error, predbat_error))
            failed = True

        # The reason solar_affine is the default over plain cyclic interpolation: on the
        # Predbat saving, which drives payback, it was +0.2% against linear's -4.4%.
        linear_system, linear_predbat = rebuild(doc, BASIS_LINEAR)
        linear_predbat_error = abs(linear_predbat - true_predbat) / abs(true_predbat) * 100
        print("  {}: linear predbat saving error {:.2f}% (solar_affine {:.2f}%)".format(label, linear_predbat_error, predbat_error))
        if predbat_error > linear_predbat_error:
            print("  ERROR: {} - solar_affine ({:.2f}%) should beat linear ({:.2f}%) on the Predbat saving, or it is not the right default".format(label, predbat_error, linear_predbat_error))
            failed = True

    if not seen:
        print("  SKIP: no annual reference fixtures found")

    return failed
