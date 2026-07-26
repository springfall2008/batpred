# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Unit tests for assembling the annual prediction results document.

Covers ``AnnualPredictor._build_results``, ``AnnualPredictor._month_scenarios`` and
``average_rate`` against hand-built month rows and samples. None of this touches the
network, a Predbat instance or a real plan run, so it is fast and registered as a
non-slow test - unlike ``test_annual_integration``, which needs a real day run to
exercise this same code from the top.
"""

from datetime import date

from annual import SCENARIO_FIELDS, SCENARIO_KEYS, AnnualPredictor, average_rate


def make_predictor():
    """Return an ``AnnualPredictor`` built from a minimal valid config.

    ``AnnualPredictor.__init__`` validates the config itself and touches no network or
    Predbat state, so this is safe to call directly in a unit test.
    """
    raw = {
        "location": {"latitude": 51.5, "longitude": -0.1},
        "solar": [{"kwp": 5.0}],
        "battery": {"size_kwh": 10.0, "inverter_kw": 5.0},
        "load": {"annual_kwh": 3800, "shape": "flat"},
        "tariff": {"rates_import": [{"rate": 28.0}]},
        "year": 2025,
    }
    return AnnualPredictor(raw)


def make_month_row(month, costs, status="ok"):
    """Build a month row with the given status and per-scenario cost_p values.

    Every other scenario field is fixed at 1.0 (or 0.5 for the two derived fields) so
    the totals in each test are easy to predict by hand.
    """
    scenarios = {}
    for key in SCENARIO_KEYS:
        entry = {field: 1.0 for field in SCENARIO_FIELDS}
        entry["cost_p"] = costs[key]
        entry["export_credit_p_estimate"] = 0.5
        entry["self_consumed_kwh"] = 0.5
        entry["self_consumed_kwh_meaningful"] = True
        scenarios[key] = entry
    row = {"month": month, "status": status, "days": 30, "standing_charge_p": 100.0, "scenarios": scenarios}
    if status == "degraded":
        row["failed_days"] = ["2025-{:02d}-15".format(month)]
    return row


def make_unavailable_row(month, reason="no rate data available"):
    """Build an 'unavailable' month row, as run() emits when a month has no usable result."""
    return {"month": month, "status": "unavailable", "reason": reason, "days": 30, "standing_charge_p": 15.0}


def test_annual_results(my_predbat):
    """Verify _build_results, _month_scenarios and average_rate against hand-built inputs."""
    failed = False
    print("**** Testing annual results assembly ****")

    print("Test: _build_results with every month ok sums totals across all months")
    predictor = make_predictor()
    months = [
        make_month_row(1, {"no_pvbat": 100.0, "without_predbat": 50.0, "with_predbat": 20.0}),
        make_month_row(2, {"no_pvbat": 200.0, "without_predbat": 80.0, "with_predbat": 30.0}),
    ]
    result = predictor._build_results(months)
    if result["annual"]["scenarios"]["no_pvbat"]["cost_p"] != 300.0:
        print("  ERROR: expected no_pvbat annual cost_p 300.0, got {}".format(result["annual"]["scenarios"]["no_pvbat"]["cost_p"]))
        failed = True
    if result["annual"]["scenarios"]["with_predbat"]["cost_p"] != 50.0:
        print("  ERROR: expected with_predbat annual cost_p 50.0, got {}".format(result["annual"]["scenarios"]["with_predbat"]["cost_p"]))
        failed = True
    if result["annual"]["months_included"] != 2 or result["annual"]["months_excluded"] != []:
        print("  ERROR: expected 2 months included and none excluded, got {} / {}".format(result["annual"]["months_included"], result["annual"]["months_excluded"]))
        failed = True
    expected_saving = 300.0 - 130.0
    if result["annual"]["savings"]["pv_battery_vs_none_p"] != expected_saving:
        print("  ERROR: expected pv_battery_vs_none_p {}, got {}".format(expected_saving, result["annual"]["savings"]["pv_battery_vs_none_p"]))
        failed = True

    print("Test: _build_results with a mix of ok and unavailable months excludes the unavailable one")
    predictor = make_predictor()
    months = [
        make_month_row(1, {"no_pvbat": 100.0, "without_predbat": 50.0, "with_predbat": 20.0}),
        make_unavailable_row(2),
        make_month_row(3, {"no_pvbat": 40.0, "without_predbat": 10.0, "with_predbat": 5.0}),
    ]
    result = predictor._build_results(months)
    if result["annual"]["scenarios"]["no_pvbat"]["cost_p"] != 140.0:
        print("  ERROR: expected no_pvbat annual cost_p 140.0 (month 2 excluded), got {}".format(result["annual"]["scenarios"]["no_pvbat"]["cost_p"]))
        failed = True
    if result["annual"]["months_included"] != 2 or result["annual"]["months_excluded"] != [2]:
        print("  ERROR: expected 2 months included and month 2 excluded, got {} / {}".format(result["annual"]["months_included"], result["annual"]["months_excluded"]))
        failed = True

    print("Test: _build_results includes a 'degraded' month (partial-sample) in totals, not excluded")
    predictor = make_predictor()
    months = [make_month_row(4, {"no_pvbat": 60.0, "without_predbat": 25.0, "with_predbat": 10.0}, status="degraded")]
    result = predictor._build_results(months)
    if result["annual"]["months_included"] != 1 or result["annual"]["months_excluded"] != []:
        print("  ERROR: expected the degraded month to be included, not excluded, got included={} excluded={}".format(result["annual"]["months_included"], result["annual"]["months_excluded"]))
        failed = True
    if result["annual"]["scenarios"]["no_pvbat"]["cost_p"] != 60.0:
        print("  ERROR: expected the degraded month's totals to be counted, got {}".format(result["annual"]["scenarios"]["no_pvbat"]["cost_p"]))
        failed = True

    print("Test: _build_results with every month unavailable does not fabricate a zero-cost year")
    predictor = make_predictor()
    months = [make_unavailable_row(1), make_unavailable_row(2, reason="no usable weather days")]
    result = predictor._build_results(months)
    if result["annual"]["scenarios"] is not None:
        print("  ERROR: expected annual scenarios to be None when nothing is included, got {}".format(result["annual"]["scenarios"]))
        failed = True
    if result["annual"]["standing_charge_p"] is not None:
        print("  ERROR: expected annual standing_charge_p to be None when nothing is included, got {}".format(result["annual"]["standing_charge_p"]))
        failed = True
    if result["annual"]["savings"] != {}:
        print("  ERROR: expected empty savings when nothing is included, got {}".format(result["annual"]["savings"]))
        failed = True
    if result["annual"]["months_included"] != 0 or result["annual"]["months_excluded"] != [1, 2]:
        print("  ERROR: expected 0 months included and both excluded, got {} / {}".format(result["annual"]["months_included"], result["annual"]["months_excluded"]))
        failed = True
    if not any("no month produced a usable result" in caveat.lower() for caveat in result["caveats"]):
        print("  ERROR: expected a caveat explaining that no month produced a usable result, got {}".format(result["caveats"]))
        failed = True

    print("Test: _month_scenarios weights uneven samples correctly")
    predictor = make_predictor()
    samples = [(date(2025, 1, 5), 3), (date(2025, 1, 20), 5)]
    day_results = [
        {key: {field: (10.0 if key == "no_pvbat" else 2.0) for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS},
        {key: {field: (2.0 if key == "no_pvbat" else 1.0) for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS},
    ]
    totals = predictor._month_scenarios(samples, day_results)
    expected_no_pvbat = 10.0 * 3 + 2.0 * 5
    if totals["no_pvbat"]["cost_p"] != expected_no_pvbat:
        print("  ERROR: expected weighted no_pvbat cost_p {}, got {}".format(expected_no_pvbat, totals["no_pvbat"]["cost_p"]))
        failed = True
    expected_with_predbat = 2.0 * 3 + 1.0 * 5
    if totals["with_predbat"]["import_kwh"] != expected_with_predbat:
        print("  ERROR: expected weighted with_predbat import_kwh {}, got {}".format(expected_with_predbat, totals["with_predbat"]["import_kwh"]))
        failed = True

    print("Test: average_rate")
    full_rates = {0: 10.0, 1: 20.0, 2: 30.0}
    if average_rate(full_rates, 3) != 20.0:
        print("  ERROR: expected average_rate of a full dict to be 20.0, got {}".format(average_rate(full_rates, 3)))
        failed = True
    partial_rates = {0: 10.0}
    if average_rate(partial_rates, 3) != 10.0:
        print("  ERROR: expected average_rate to ignore minutes missing from the dict, got {}".format(average_rate(partial_rates, 3)))
        failed = True
    if average_rate({}, 10) != 0.0:
        print("  ERROR: expected average_rate of an empty dict to be 0.0 rather than raising, got {}".format(average_rate({}, 10)))
        failed = True

    return failed
