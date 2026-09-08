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

import inspect
from datetime import date

from annual import INCLUDED_STATUSES, SCENARIO_FIELDS, SCENARIO_KEYS, AnnualPredictor, _billed_result, _capture_plan, _run_scenarios, average_rate, run_day, validate_config
from prediction import Prediction
from tests.test_infra import reset_inverter


def base_config():
    """Return the same minimal valid annual config literal ``make_predictor()`` uses.

    Kept as a standalone helper (rather than only living inside ``make_predictor()``) so
    tests that need the raw dict itself - for example to pass through ``validate_config()``
    directly - do not have to invent their own values.
    """
    return {
        "location": {"latitude": 51.5, "longitude": -0.1},
        "solar": [{"kwp": 5.0}],
        "battery": {"size_kwh": 10.0, "inverter_kw": 5.0},
        "load": {"annual_kwh": 3800, "shape": "flat"},
        "tariff": {"rates_import": [{"rate": 28.0}]},
        "year": 2025,
    }


def make_predictor():
    """Return an ``AnnualPredictor`` built from a minimal valid config.

    ``AnnualPredictor.__init__`` validates the config itself and touches no network or
    Predbat state, so this is safe to call directly in a unit test.
    """
    return AnnualPredictor(base_config())


def make_month_row(month, costs, status="ok"):
    """Build a month row with the given status and per-scenario cost_p values.

    Every other scenario field is fixed at 1.0 (or 0.5 for the derived export credit) so
    the totals in each test are easy to predict by hand.
    """
    scenarios = {}
    for key in SCENARIO_KEYS:
        entry = {field: 1.0 for field in SCENARIO_FIELDS}
        entry["cost_p"] = costs[key]
        entry["export_credit_p_estimate"] = 0.5
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
        make_month_row(1, {"no_pvbat": 100.0, "pv_only": 75.0, "without_predbat": 50.0, "with_predbat": 20.0}),
        make_month_row(2, {"no_pvbat": 200.0, "pv_only": 140.0, "without_predbat": 80.0, "with_predbat": 30.0}),
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
        make_month_row(1, {"no_pvbat": 100.0, "pv_only": 75.0, "without_predbat": 50.0, "with_predbat": 20.0}),
        make_unavailable_row(2),
        make_month_row(3, {"no_pvbat": 40.0, "pv_only": 25.0, "without_predbat": 10.0, "with_predbat": 5.0}),
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
    months = [make_month_row(4, {"no_pvbat": 60.0, "pv_only": 42.5, "without_predbat": 25.0, "with_predbat": 10.0}, status="degraded")]
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

    print("Test: a degraded month's survivor is reweighted to represent the full month, not its original share")
    # select_samples() would have given each of an original two samples in a 31 day month a
    # weight of 31/2 = 15.5. One of them failed run_day() and was dropped. Naively summing the
    # survivor at its original 15.5 weight would silently under-count the month by half; the
    # fix must recompute the survivor's weight from the surviving count (31/1 = 31) so the
    # month's total still represents all 31 days, using the ONE plan that actually ran.
    predictor = make_predictor()
    original_samples = [(date(2025, 3, 10), 15.5), (date(2025, 3, 24), 15.5)]
    surviving_samples = original_samples[:1]
    day_result = {key: {field: 2.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}
    reweighted_samples = predictor._reweight_survivors(surviving_samples, days_in_month=31)
    totals = predictor._month_scenarios(reweighted_samples, [day_result])
    expected_reweighted = 2.0 * 31
    wrong_if_unweighted = 2.0 * 15.5
    if totals["no_pvbat"]["cost_p"] != expected_reweighted:
        print("  ERROR: expected the degraded month's total to be the survivor's figure times the full 31 days ({}), got {} (original-weight total would have been {})".format(expected_reweighted, totals["no_pvbat"]["cost_p"], wrong_if_unweighted))
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

    print("Test: _synthesised_sides filters a run-wide fallback_months set down to the one requested month")
    fallback_months = {(2025, 3, "export"), (2025, 3, "import"), (2025, 4, "export"), (2024, 3, "export")}
    sides = predictor._synthesised_sides(fallback_months, 2025, 3)
    if sides != ["export", "import"]:
        print("  ERROR: expected ['export', 'import'] for (2025, 3), got {}".format(sides))
        failed = True
    if predictor._synthesised_sides(fallback_months, 2025, 5) != []:
        print("  ERROR: a month with no fallback entries should return an empty list, got {}".format(predictor._synthesised_sides(fallback_months, 2025, 5)))
        failed = True

    print("Test: _tariff_fallback_caveats reports nothing when the tariff needed neither fallback")
    if AnnualPredictor._tariff_fallback_caveats(set(), set(), 2025) != []:
        print("  ERROR: expected no caveats when both sets are empty, got {}".format(AnnualPredictor._tariff_fallback_caveats(set(), set(), 2025)))
        failed = True

    print("Test: _tariff_fallback_caveats names the affected months for a current-rates fallback")
    caveats = AnnualPredictor._tariff_fallback_caveats({(2025, 7, "import"), (2025, 7, "export")}, set(), 2025)
    if len(caveats) != 1:
        print("  ERROR: expected exactly one caveat for a fallback-only case, got {}".format(caveats))
        failed = True
    elif "2025-07" not in caveats[0] or "today's rates" not in caveats[0]:
        print("  ERROR: expected the fallback caveat to name the month and explain what happened, got {!r}".format(caveats[0]))
        failed = True

    print("Test: _tariff_fallback_caveats separately reports a month where export could not be priced at all")
    caveats = AnnualPredictor._tariff_fallback_caveats(set(), {(2025, 9)}, 2025)
    if len(caveats) != 1:
        print("  ERROR: expected exactly one caveat for an unpaid-export-only case, got {}".format(caveats))
        failed = True
    elif "2025-09" not in caveats[0] or "zero" not in caveats[0]:
        print("  ERROR: expected the unpaid-export caveat to name the month and say export was priced at zero, got {!r}".format(caveats[0]))
        failed = True

    print("Test: _tariff_fallback_caveats returns both caveats together when both conditions apply")
    caveats = AnnualPredictor._tariff_fallback_caveats({(2025, 7, "import")}, {(2025, 9)}, 2025)
    if len(caveats) != 2:
        print("  ERROR: expected both caveats when both sets are non-empty, got {}".format(caveats))
        failed = True

    print("Test: _tariff_fallback_caveats excludes a spill-month entry from the following year (fetch_month(year + 1, 1) can itself hit a fallback)")
    # December's spill fetch loads January of the *next* year purely so the last sampled
    # day's 48 hour plan has rates to spill into - there is no row for that month
    # anywhere in this year's results document, so a caveat naming "2026-01" inside a
    # 2025 report would refer to nothing a reader can find.
    caveats = AnnualPredictor._tariff_fallback_caveats({(2025, 12, "import"), (2026, 1, "import")}, {(2026, 1)}, 2025)
    if len(caveats) != 1:
        print("  ERROR: expected only the fallback caveat (for 2025-12), the spill month's entries should be filtered out entirely, got {}".format(caveats))
        failed = True
    elif "2026-01" in caveats[0] or "2025-12" not in caveats[0]:
        print("  ERROR: expected the caveat to name 2025-12 but not the spill month 2026-01, got {!r}".format(caveats[0]))
        failed = True

    print("Test: _p10_fallback_caveat filters an all-months fallback set down to the requested subset")
    # AnnualWeather._derive_p10_ratios always iterates the full calendar (see annual_weather.py),
    # so on a real run with annual.months: [7] this set covers every one of the 12 months, not
    # just the one requested - _p10_fallback_caveat is what stops that being reported verbatim.
    caveat = AnnualPredictor._p10_fallback_caveat(set(range(1, 13)), [7], 0.7)
    if caveat is None or "Months [7]" not in caveat:
        print("  ERROR: expected the caveat to name only the requested month 7, got {!r}".format(caveat))
        failed = True

    print("Test: _p10_fallback_caveat returns None (no caveat at all) when none of the requested months fell back")
    caveat = AnnualPredictor._p10_fallback_caveat({1, 2, 3}, [7], 0.7)
    if caveat is not None:
        print("  ERROR: expected no caveat when none of the requested months fell back, got {!r}".format(caveat))
        failed = True

    print("Test: _p10_fallback_caveat is unchanged for a full-year request")
    caveat = AnnualPredictor._p10_fallback_caveat({3, 9}, list(range(1, 13)), 0.7)
    if caveat is None or "Months [3, 9]" not in caveat:
        print("  ERROR: expected months 3 and 9 named unchanged for a full-year request, got {!r}".format(caveat))
        failed = True

    print("Test: the annual block carries a cost breakdown")
    predictor = make_predictor()
    months = [make_month_row(month, {"no_pvbat": 1000.0, "pv_only": 700.0, "without_predbat": 600.0, "with_predbat": 400.0}) for month in range(1, 13)]
    result = predictor._build_results(months)
    costs = result["annual"]["costs"]
    # base_config()'s solar is a single 5.0 kWp array and its battery is 10.0 kWh.
    if abs(costs["total_kwp"] - 5.0) > 0.001 or abs(costs["battery_kwh"] - 10.0) > 0.001:
        print("  ERROR: the cost block should reflect the configured system, got {}".format(costs))
        failed = True
    if abs(costs["battery_gbp"] - 3500.0) > 0.01:
        print("  ERROR: a 10 kWh battery should cost 3500, got {}".format(costs["battery_gbp"]))
        failed = True
    if abs(costs["total_gbp"] - (costs["pv_gbp"] + costs["battery_gbp"])) > 0.01:
        print("  ERROR: total should be pv plus battery, got {}".format(costs))
        failed = True

    print("Test: a full twelve months produces payback for all three options")
    payback = result["annual"]["payback"]
    if not payback.get("available"):
        print("  ERROR: twelve months should produce payback, got {}".format(payback))
        failed = True
    for key in ["pv_only", "pv_battery", "pv_battery_predbat"]:
        if key not in payback:
            print("  ERROR: payback should include {}, got {}".format(key, list(payback)))
            failed = True

    print("Test: a full twelve month document carries no months_requested key at all")
    # Load-bearing for default-off (see _build_results' docstring): an existing full-year
    # document/caller must gain no new key from this.
    if "months_requested" in result:
        print("  ERROR: a full twelve month document should not carry months_requested, got {!r}".format(result.get("months_requested")))
        failed = True

    print("Test: a partial year refuses payback rather than extrapolating")
    partial_months = [make_month_row(month, {"no_pvbat": 1000.0, "pv_only": 700.0, "without_predbat": 600.0, "with_predbat": 400.0}) for month in range(1, 12)]
    partial_months.append(make_unavailable_row(12))
    partial = make_predictor()._build_results(partial_months)
    if partial["annual"]["payback"].get("available") is not False:
        print("  ERROR: eleven usable months should refuse payback, got {}".format(partial["annual"]["payback"]))
        failed = True
    if partial["annual"]["costs"]["total_gbp"] <= 0:
        print("  ERROR: costs should still be reported even when payback cannot be")
        failed = True
    partial_reason = str(partial["annual"]["payback"].get("reason", ""))
    if "could be modelled" not in partial_reason or "missing months are named in the caveats" not in partial_reason.lower():
        print("  ERROR: a genuine full-year shortfall should keep the original 'only N of 12 months could be modelled' wording, got {!r}".format(partial_reason))
        failed = True
    if "deliberately covered" in partial_reason:
        print("  ERROR: a genuine full-year shortfall must not use the deliberate-subset wording, got {!r}".format(partial_reason))
        failed = True

    print("Test: a deliberate single-month run's payback caveat says so, not that months are missing")
    # From a real acceptance run of annual.months: [7]: build_payback used to say "only 1 of
    # 12 months could be modelled. The missing months are named in the caveats." - which reads
    # as a broken model on the hosted page, when in fact only one month was ever requested.
    subset_config = base_config()
    subset_config["months"] = [7]
    subset_predictor = AnnualPredictor(subset_config)
    subset_months = [make_month_row(7, {"no_pvbat": 1000.0, "pv_only": 700.0, "without_predbat": 600.0, "with_predbat": 400.0})]
    subset_result = subset_predictor._build_results(subset_months)
    if subset_result["annual"]["payback"].get("available") is not False:
        print("  ERROR: a single modelled month should still refuse payback, got {}".format(subset_result["annual"]["payback"]))
        failed = True
    subset_reason = str(subset_result["annual"]["payback"].get("reason", ""))
    if "deliberately covered" not in subset_reason or "1" not in subset_reason:
        print("  ERROR: expected the payback reason to say this run deliberately covered 1 month, got {!r}".format(subset_reason))
        failed = True
    if "could not be modelled" in subset_reason or "missing" in subset_reason.lower():
        print("  ERROR: a deliberate single-month run's payback reason must not describe it as a failure to model months, got {!r}".format(subset_reason))
        failed = True
    matching_run_wide_caveats = [caveat for caveat in subset_predictor.caveats if "deliberately covered" in caveat]
    if len(matching_run_wide_caveats) != 1:
        print("  ERROR: expected exactly one run-wide caveat naming the deliberate subset, got {}".format(subset_predictor.caveats))
        failed = True

    print("Test: a plain single-tariff document from an explicit month subset carries months_requested")
    # This is what actually fixes the "Based on 1 of 12 months." / "this run deliberately
    # covered 1 of 12 month(s)." contradiction above: annual_cli._format_single_table only
    # switches wording once the document itself names how many months were requested.
    if subset_result.get("months_requested") != [7]:
        print("  ERROR: a --months [7] document should carry months_requested=[7], got {!r}".format(subset_result.get("months_requested")))
        failed = True

    print("Test: INCLUDED_STATUSES covers planned, degraded and interpolated months")
    if sorted(INCLUDED_STATUSES) != ["degraded", "interpolated", "ok"]:
        print("  ERROR: INCLUDED_STATUSES should be ok/degraded/interpolated, got {!r}".format(INCLUDED_STATUSES))
        failed = True

    print("Test: an interpolated month counts toward the annual totals")
    # The whole point of fast mode: these months were never planned, but excluding them
    # would report a four month year rather than a twelve month one.
    predictor = make_predictor()
    months = [
        make_month_row(1, {"no_pvbat": 100.0, "pv_only": 75.0, "without_predbat": 50.0, "with_predbat": 20.0}),
        make_month_row(2, {"no_pvbat": 200.0, "pv_only": 140.0, "without_predbat": 80.0, "with_predbat": 30.0}, status="interpolated"),
    ]
    result = predictor._build_results(months)
    if result["annual"]["months_included"] != 2:
        print("  ERROR: an interpolated month should be included, got months_included {}".format(result["annual"]["months_included"]))
        failed = True
    if result["annual"]["months_excluded"] != []:
        print("  ERROR: an interpolated month must not be excluded, got {}".format(result["annual"]["months_excluded"]))
        failed = True
    if result["annual"]["scenarios"]["no_pvbat"]["cost_p"] != 300.0:
        print("  ERROR: the interpolated month's cost should be in the total, expected 300.0, got {}".format(result["annual"]["scenarios"]["no_pvbat"]["cost_p"]))
        failed = True

    if test_annual_debug_flag():
        failed = True

    if test_annual_debug_capture(my_predbat):
        failed = True

    return failed


def test_annual_debug_flag():
    """Verify the debug flag defaults off and is coerced to a bool when set."""
    failed = False
    print("Test: debug defaults to False")
    config = validate_config(base_config())
    if config["debug"] is not False:
        print("  ERROR: debug should default to False, got {!r}".format(config["debug"]))
        failed = True

    print("Test: debug is coerced to a bool, so a form's 'on' string does not become a truthy string")
    raw = base_config()
    raw["debug"] = "on"
    config = validate_config(raw)
    if config["debug"] is not True:
        print("  ERROR: debug should coerce to True, got {!r}".format(config["debug"]))
        failed = True

    print("Test: debug is not echoed into the scrubbed raw config as a secret")
    if config["raw"].get("debug") != "on":
        print("  ERROR: raw config should retain the submitted value")
        failed = True

    print("Test: debug='false' (a non-empty string) is coerced to False, not the bare bool('false') == True bug")
    # Plain Python's bool("false") is True (any non-empty string is truthy), which is
    # exactly the trap _coerce_bool exists to avoid: an explicit, well-meaning "debug:
    # false" in a submitted config must not silently turn debug mode on.
    raw = base_config()
    raw["debug"] = "false"
    config = validate_config(raw)
    if config["debug"] is not False:
        print("  ERROR: debug='false' should coerce to False, got {!r}".format(config["debug"]))
        failed = True

    print("Test: a real bool for debug passes through unchanged")
    raw = base_config()
    raw["debug"] = True
    config = validate_config(raw)
    if config["debug"] is not True:
        print("  ERROR: debug=True should stay True, got {!r}".format(config["debug"]))
        failed = True
    return failed


def test_annual_debug_capture(my_predbat):
    """Verify ``_capture_plan()`` returns the structure the web plan renderer requires.

    ``_run_scenarios()`` needs a full weather/tariff/load-source rig to drive from a unit
    test (see ``test_annual_integration.py``, registered as slow, which also carries the
    regression guards that need that rig: identical billed figures with and without
    capture, and that a capturing run never leaves ``predbat.debug_enable`` on). This
    exercises the capture helper directly instead, against the same kind of minimal plan
    state ``test_plan_json_rate_adjust.py`` builds - empty charge/export windows, no
    search - drawing on the SAME two-call sequence production uses: ``_billed_result()``
    with no ``save`` argument (unchanged since before this feature), followed by
    ``_capture_plan()``, which does its own internal ``save="best"`` re-run to populate
    ``predict_*_best`` without leaking a standing charge into the billed figures above.
    An earlier version of both this test and the production code threaded ``save``
    into ``_billed_result()`` itself; that was wrong on two counts, in order: first, it
    was simply absent from production (``_billed_result()`` never passed ``save`` at
    all, so ``_capture_plan()`` raised ``AttributeError`` there), and once that was
    fixed by threading ``save="best"`` into the billed run, that in turn silently added
    a standing charge into ``cost_p`` (``prediction.py``'s ``enable_standing_charge``),
    which the annual engine already accounts for separately. Both regressions are
    covered below and in ``test_annual_integration.py``.
    """
    failed = False
    print("Test: _capture_plan returns a renderer-ready plan with non-empty rows")

    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 24 * 60
    my_predbat.debug_enable = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.num_inverters = 1
    my_predbat.reserve = 0.5

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0
        load_step[minute] = 0.5 / (60 / 5)
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step, soc_kw=my_predbat.soc_kw, soc_max=my_predbat.soc_max)

    # Empty windows/limits, exactly as _run_scenarios() passes for its "no system" scenario.
    my_predbat.charge_limit_best = []
    my_predbat.charge_window_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []

    # The production call sequence: _run_scenarios() always calls _billed_result() with no
    # save argument, then - only under debug - _capture_plan(), which does its own internal
    # save="best" run. billed_before/billed_after bracket the capture so the regression
    # guard below can prove the capture changed nothing about the billed numbers.
    billed_before = _billed_result(my_predbat, my_predbat.end_record, pv_step)

    plan = _capture_plan(my_predbat, pv_step, pv_step, load_step, load_step, my_predbat.end_record)
    if not plan.get("rows"):
        print("  ERROR: expected a non-empty 'rows' list, got {!r}".format(plan.get("rows")))
        failed = True
    if "soc_max" not in plan:
        print("  ERROR: expected 'soc_max' in the captured plan")
        failed = True
    if "end_record" not in plan:
        print("  ERROR: expected 'end_record' in the captured plan")
        failed = True

    print("Test: capturing a plan does not change the billed figures (no standing charge leak)")
    # _capture_plan()'s internal save="best" run must not perturb anything _billed_result()
    # reads (charge/export windows and limits, soc_kw, predbat.prediction), so an identical
    # _billed_result() call straight after capture must reproduce the exact same figures -
    # this is the direct, single-scenario mirror of test_annual_integration.py's regression
    # guard, which proves the same thing through the full three-scenario production path.
    billed_after = _billed_result(my_predbat, my_predbat.end_record, pv_step)
    for field in billed_before:
        if billed_before[field] != billed_after[field]:
            print("  ERROR: {} = {} before capture but {} after - capturing a plan must not change the billed figures".format(field, billed_before[field], billed_after[field]))
            failed = True

    print("Test: capturing plans does not leak predbat.debug_enable on")
    if my_predbat.debug_enable is not False:
        print("  ERROR: predbat.debug_enable should still be False after a capturing run, got {!r}".format(my_predbat.debug_enable))
        failed = True

    print("Test: plans=None leaves the non-debug path untouched")
    # _run_scenarios() needs a full weather/tariff/load-source rig to drive directly (see
    # test_annual_integration.py, registered as slow), so this checks the contract that
    # actually keeps a non-debug run untouched: plans defaults to None on both functions
    # that thread it through, so a caller that never opts in triggers no capture at all.
    scenarios_signature = inspect.signature(_run_scenarios)
    if scenarios_signature.parameters["plans"].default is not None:
        print("  ERROR: _run_scenarios' plans parameter should default to None, got {!r}".format(scenarios_signature.parameters["plans"].default))
        failed = True
    day_signature = inspect.signature(run_day)
    if day_signature.parameters["plans"].default is not None:
        print("  ERROR: run_day's plans parameter should default to None, got {!r}".format(day_signature.parameters["plans"].default))
        failed = True

    return failed
