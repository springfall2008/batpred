# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Unit tests for the annual install-cost and payback model.

Everything here is pure arithmetic over plain dicts - no network, no Predbat
instance, no plan run - so it is fast and registered as a non-slow test.
"""

from annual_costs import battery_cost_gbp, build_costs, build_payback, payback_row, pv_cost_gbp, pv_rate_gbp_per_kwp, resolve_costs


def close(actual, expected, tolerance=0.01):
    """Return True when two floats agree to within a tolerance."""
    return abs(actual - expected) <= tolerance


def test_annual_costs(my_predbat):
    """Verify the cost bands, the minimum, and the payback arithmetic.

    ``my_predbat`` is unused: this module is pure arithmetic with no Predbat state, but
    the test runner calls every registered test with the shared instance uniformly.
    """
    failed = False
    print("**** Testing annual cost and payback model ****")
    settings = resolve_costs(None)

    print("Test: the band rate sits exactly on each published median at its anchor")
    for kwp, expected in [(2.0, 1780.0), (7.0, 1697.0), (30.0, 1262.0)]:
        rate = pv_rate_gbp_per_kwp(kwp, settings)
        if not close(rate, expected):
            print("  ERROR: {} kWp should be {} per kWp, got {}".format(kwp, expected, rate))
            failed = True

    print("Test: the rate interpolates between anchors rather than stepping")
    # A step function is what produces the 4.0/4.1 kWp discontinuity this design exists
    # to avoid, so assert an actual in-between value, not merely "different".
    if not close(pv_rate_gbp_per_kwp(4.5, settings), 1738.50):
        print("  ERROR: 4.5 kWp should interpolate to 1738.50, got {}".format(pv_rate_gbp_per_kwp(4.5, settings)))
        failed = True

    print("Test: the rate is clamped flat outside the anchor span")
    if not close(pv_rate_gbp_per_kwp(0.5, settings), 1780.0) or not close(pv_rate_gbp_per_kwp(45.0, settings), 1262.0):
        print("  ERROR: the rate should clamp to 1780 below 2 kWp and 1262 above 30 kWp")
        failed = True

    print("Test: total PV cost never decreases as the system grows")
    # This is the property that motivated interpolation over a step function.
    previous = -1.0
    size = 0.1
    while size <= 50.0:
        cost = pv_cost_gbp(size, settings)
        if cost < previous - 0.001:
            print("  ERROR: PV cost fell from {} to {} at {} kWp".format(previous, cost, size))
            failed = True
            break
        previous = cost
        size += 0.1

    print("Test: the minimum applies to a small system and not to a large one")
    if not close(pv_cost_gbp(1.0, settings), 2500.0):
        print("  ERROR: a 1 kWp system should cost the 2500 minimum, got {}".format(pv_cost_gbp(1.0, settings)))
        failed = True
    if not close(pv_cost_gbp(5.0, settings), 8651.0, tolerance=1.0):
        print("  ERROR: a 5 kWp system should cost about 8651, got {}".format(pv_cost_gbp(5.0, settings)))
        failed = True

    print("Test: no PV and no battery cost nothing, rather than the minimum")
    if pv_cost_gbp(0, settings) != 0.0:
        print("  ERROR: zero PV should cost nothing, got {}".format(pv_cost_gbp(0, settings)))
        failed = True
    if battery_cost_gbp(0, settings) != 0.0:
        print("  ERROR: zero battery should cost nothing, got {}".format(battery_cost_gbp(0, settings)))
        failed = True

    print("Test: battery cost is the install fee plus the per-kWh rate")
    if not close(battery_cost_gbp(9.5, settings), 500.0 + 300.0 * 9.5):
        print("  ERROR: a 9.5 kWh battery should cost 3350, got {}".format(battery_cost_gbp(9.5, settings)))
        failed = True

    print("Test: custom settings override every default")
    custom = resolve_costs({"battery_install_gbp": 0, "battery_per_kwh_gbp": 200, "pv_minimum_gbp": 0, "pv_rate_small_gbp_per_kwp": 1000, "pv_rate_medium_gbp_per_kwp": 900, "pv_rate_large_gbp_per_kwp": 800, "predbat_annual_gbp": 99})
    if not close(battery_cost_gbp(10, custom), 2000.0) or not close(pv_rate_gbp_per_kwp(2.0, custom), 1000.0) or custom["predbat_annual_gbp"] != 99:
        print("  ERROR: custom cost settings were not honoured: {}".format(custom))
        failed = True

    print("Test: NaN and Infinity are rejected, not accepted as valid costs")
    # float("nan") < 0 is False, so the existing "must not be negative" guard alone lets
    # NaN straight through; a NaN annual_saving_gbp then makes payback_row's net <= 0
    # gate also False (NaN comparisons are always False), so it reports "pays_back:
    # True, years: nan" and the page would render "nan years" as though it were a real
    # answer. math.isfinite must catch both NaN and +/-Infinity before that can happen.
    for bad in [float("nan"), float("inf"), float("-inf")]:
        try:
            resolve_costs({"predbat_annual_gbp": bad})
            print("  ERROR: resolve_costs should reject a non-finite cost ({}), it was accepted".format(bad))
            failed = True
        except ValueError:
            pass

    print("Test: solar-only and whole-system quotes replace the modelled costs")
    quoted = resolve_costs({"quoted_pv_gbp": 7000.0, "quoted_total_gbp": 11200.0})
    costs = build_costs(5.0, 9.5, quoted)
    if not close(costs["pv_gbp"], 7000.0) or not close(costs["total_gbp"], 11200.0):
        print("  ERROR: both quotes should be used as given, got {}".format(costs))
        failed = True
    # The battery is the REMAINDER, so a user with one whole-system quote does no sums.
    if not close(costs["battery_gbp"], 4200.0):
        print("  ERROR: the battery should be the difference between the two quotes, got {}".format(costs))
        failed = True
    if not costs["pv_quoted"] or not costs["battery_quoted"]:
        print("  ERROR: a quoted figure should be flagged so the UI can label it, got {}".format(costs))
        failed = True

    print("Test: a whole-system quote alone still leaves the PV-only payback on the modelled solar")
    # The common case: one quote for the installation, nothing broken out for solar.
    whole = build_costs(5.0, 9.5, resolve_costs({"quoted_total_gbp": 11200.0}))
    modelled_pv = pv_cost_gbp(5.0, resolve_costs(None))
    if not close(whole["pv_gbp"], modelled_pv):
        print("  ERROR: with no solar-only quote the PV cost should still be modelled, got {}".format(whole))
        failed = True
    if not close(whole["total_gbp"], 11200.0) or not close(whole["battery_gbp"], 11200.0 - modelled_pv):
        print("  ERROR: the total should be the quote and the battery its remainder, got {}".format(whole))
        failed = True
    if whole["pv_quoted"]:
        print("  ERROR: the solar was not quoted here and must not be labelled as such, got {}".format(whole))
        failed = True

    print("Test: a solar-only quote alone still leaves the battery modelled")
    solar_only = build_costs(5.0, 9.5, resolve_costs({"quoted_pv_gbp": 7000.0}))
    if not close(solar_only["pv_gbp"], 7000.0) or not close(solar_only["battery_gbp"], battery_cost_gbp(9.5, resolve_costs(None))):
        print("  ERROR: a solar-only quote should leave the battery modelled, got {}".format(solar_only))
        failed = True

    print("Test: a whole-system quote below the solar figure cannot make the battery negative")
    # A contradiction only the user can resolve, but it must not produce a negative cost
    # or a total that disagrees with its own parts.
    contradictory = build_costs(5.0, 9.5, resolve_costs({"quoted_pv_gbp": 9000.0, "quoted_total_gbp": 8000.0}))
    if contradictory["battery_gbp"] < 0:
        print("  ERROR: the battery cost must not go negative, got {}".format(contradictory))
        failed = True
    if not close(contradictory["total_gbp"], contradictory["pv_gbp"] + contradictory["battery_gbp"]):
        print("  ERROR: the total must always equal its parts, got {}".format(contradictory))
        failed = True

    print("Test: a retired battery-only quote key is ignored rather than rejected or misread")
    # An earlier revision asked for a battery-only price. A config saved against it must
    # still load, and must NOT have that figure read as a whole-system total.
    legacy = build_costs(5.0, 9.5, resolve_costs({"quoted_battery_gbp": 4200.0}))
    if not close(legacy["pv_gbp"], modelled_pv) or legacy["battery_quoted"]:
        print("  ERROR: the retired key should be ignored entirely, got {}".format(legacy))
        failed = True

    print("Test: a zero quote means 'not quoted', not 'free'")
    unquoted = build_costs(5.0, 9.5, resolve_costs({"quoted_pv_gbp": 0, "quoted_total_gbp": 0}))
    if unquoted["pv_gbp"] <= 0 or unquoted["battery_gbp"] <= 0 or unquoted["pv_quoted"] or unquoted["battery_quoted"]:
        print("  ERROR: a zero quote should fall back to the model, got {}".format(unquoted))
        failed = True

    print("Test: a quoted PV price drops the £/kWp note, which no longer describes it")
    if build_costs(5.0, 9.5, quoted)["pv_rate_gbp_per_kwp"] != 0.0:
        print("  ERROR: a quoted PV cost has no modelled rate to quote alongside it")
        failed = True

    print("Test: payback divides capital by the annual saving")
    row = payback_row(10000.0, 1000.0)
    if not row["pays_back"] or not close(row["years"], 10.0):
        print("  ERROR: 10000 capital against 1000 a year should pay back in 10 years, got {}".format(row))
        failed = True

    print("Test: a zero or negative saving does not pay back")
    for saving in [0.0, -50.0]:
        row = payback_row(10000.0, saving)
        if row["pays_back"] or row.get("years") is not None:
            print("  ERROR: a saving of {} must not produce a payback period, got {}".format(saving, row))
            failed = True

    print("Test: a recurring cost reduces the saving and lengthens payback")
    row = payback_row(10000.0, 1000.0, recurring_gbp=100.0)
    if not close(row["years"], 11.111) or not close(row["annual_saving_gbp"], 900.0) or not close(row["gross_annual_saving_gbp"], 1000.0):
        print("  ERROR: a 100/year fee should give 900 net and 11.11 years, got {}".format(row))
        failed = True

    print("Test: a recurring cost exceeding the saving means it never pays back")
    # The case a "subscription as capital" implementation gets wrong: treating the fee as
    # a one-off would still show a payback period here.
    row = payback_row(10000.0, 80.0, recurring_gbp=100.0)
    if row["pays_back"]:
        print("  ERROR: a fee larger than the saving must not pay back, got {}".format(row))
        failed = True

    print("Test: payback is refused on a partial year rather than extrapolated")
    scenarios = {"no_pvbat": {"cost_p": 200000.0}, "pv_only": {"cost_p": 130000.0}, "without_predbat": {"cost_p": 110000.0}, "with_predbat": {"cost_p": 80000.0}}
    costs = build_costs(5.0, 9.5, settings)
    partial = build_payback(scenarios, costs, 11, settings)
    if partial.get("available") is not False or "11" not in str(partial.get("reason", "")):
        print("  ERROR: 11 months should refuse payback and say why, got {}".format(partial))
        failed = True

    print("Test: a missing scenario is refused rather than treated as a zero saving")
    # Reachable when a stored run predates the pv_only scenario, or an engine change
    # drops one - .get(key, {}).get("cost_p", baseline) would silently read this as no
    # saving at all ("never pays back"), which is a different claim to "cannot be priced".
    incomplete = {"no_pvbat": {"cost_p": 200000.0}, "without_predbat": {"cost_p": 110000.0}, "with_predbat": {"cost_p": 80000.0}}
    missing = build_payback(incomplete, costs, 12, settings)
    if missing.get("available") is not False or "pv_only" not in str(missing.get("reason", "")):
        print("  ERROR: a missing pv_only scenario should refuse payback and name it, got {}".format(missing))
        failed = True

    print("Test: a full year produces all three payback rows")
    full = build_payback(scenarios, costs, 12, settings)
    for key, capital in [("pv_only", costs["pv_gbp"]), ("pv_battery", costs["total_gbp"]), ("pv_battery_predbat", costs["total_gbp"])]:
        if key not in full:
            print("  ERROR: {} row missing from payback, got {}".format(key, full))
            failed = True
        elif not close(full[key]["capital_gbp"], capital):
            print("  ERROR: {} should use capital {}, got {}".format(key, capital, full[key]["capital_gbp"]))
            failed = True
    # no_pvbat 200000p - pv_only 130000p = 70000p = 700 GBP a year against PV-only capital
    if not close(full["pv_only"]["annual_saving_gbp"], 700.0):
        print("  ERROR: the PV-only saving should be 700 a year, got {}".format(full["pv_only"]["annual_saving_gbp"]))
        failed = True

    print("Test: the Predbat fee changes only the Predbat row")
    paid = build_payback(scenarios, costs, 12, resolve_costs({"predbat_annual_gbp": 100}))
    if not close(paid["pv_only"]["annual_saving_gbp"], full["pv_only"]["annual_saving_gbp"]):
        print("  ERROR: the Predbat fee must not touch the PV-only row")
        failed = True
    if not close(paid["pv_battery"]["annual_saving_gbp"], full["pv_battery"]["annual_saving_gbp"]):
        print("  ERROR: the Predbat fee must not touch the PV+battery row")
        failed = True
    if not close(paid["pv_battery_predbat"]["annual_saving_gbp"], full["pv_battery_predbat"]["annual_saving_gbp"] - 100.0):
        print("  ERROR: the Predbat fee should reduce the Predbat row's saving by 100")
        failed = True
    if not close(paid["pv_battery_predbat"]["capital_gbp"], full["pv_battery_predbat"]["capital_gbp"]):
        print("  ERROR: the Predbat fee is recurring and must not change capital")
        failed = True

    if failed:
        print("**** ERROR: annual cost tests failed ****")
    return failed
