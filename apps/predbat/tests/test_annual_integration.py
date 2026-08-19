# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Integration tests for the annual prediction day runner.

These run real Predbat plans, so they are registered as slow.
"""

from datetime import date, datetime

import pytz

from annual import DAY_MINUTES, DEFAULT_CAR_RATE_KW, MAX_SESSIONS_PER_WEEK, PLAN_MINUTES, SCENARIO_FIELDS, SCENARIO_KEYS, _run_scenarios, car_charging_schedule, run_day, validate_config
from annual_load import SyntheticLoadProfile
from tests.test_infra import reset_inverter

SOLAR_CURVE = [0, 0, 0, 0, 0, 0, 0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0, 0.95, 0.85, 0.7, 0.5, 0.3, 0.1, 0, 0, 0, 0, 0]


class StubWeather:
    """A WeatherYear stand-in with a fixed daily solar curve and a forecast multiplier."""

    def __init__(self, peak_kw, forecast_multiplier=1.0, p10_ratio_value=0.8):
        """Configure the actual peak power and how much the forecast overstates it."""
        self.peak_kw = peak_kw
        self.forecast_multiplier = forecast_multiplier
        self.p10_ratio_value = p10_ratio_value

    def _series(self, midnight_utc, minutes, scale):
        """Build a per-minute kWh series from the fixed daily curve."""
        result = {}
        for minute in range(minutes):
            hour = (minute // 60) % 24
            result[minute] = (self.peak_kw * SOLAR_CURVE[hour] * scale) / 60.0
        return result

    def pv_minutes(self, series, midnight_utc, minutes):
        """Return the actual or forecast per-minute series."""
        return self._series(midnight_utc, minutes, 1.0 if series == "actual" else self.forecast_multiplier)

    def pv_minutes_p10(self, midnight_utc, minutes, month):
        """Return the P10 series, the forecast scaled by the month ratio."""
        return self._series(midnight_utc, minutes, self.forecast_multiplier * self.p10_ratio_value)

    def has_actual(self, day):
        """Every day has data in this stub."""
        return True

    def daily_actual_kwh(self, day):
        """Return the fixed daily total."""
        return sum(self._series(None, DAY_MINUTES, 1.0).values())

    def p10_ratio(self, month):
        """Return the fixed P10 ratio."""
        return self.p10_ratio_value


class StubTariff:
    """A tariff stand-in with a cheap overnight band and an expensive evening peak."""

    def __init__(self, cheap=7.0, normal=28.0, peak=45.0, export=15.0):
        """Configure the four rate levels."""
        self.cheap = cheap
        self.normal = normal
        self.peak = peak
        self.export = export
        self.standing_charge_p_per_day = 50.0

    def rates_for(self, midnight_utc, minutes):
        """Return (import, export) rate dicts keyed by absolute minute."""
        rate_import = {}
        rate_export = {}
        for minute in range(minutes):
            in_day = minute % DAY_MINUTES
            if 30 <= in_day < 330:
                rate_import[minute] = self.cheap
            elif 16 * 60 <= in_day < 19 * 60:
                rate_import[minute] = self.peak
            else:
                rate_import[minute] = self.normal
            rate_export[minute] = self.export
        return rate_import, rate_export

    def month_available(self, year, month):
        """Always available."""
        return True


def make_config(with_car=False):
    """Return a validated annual config for the integration tests."""
    raw = {
        "location": {"latitude": 51.5, "longitude": -0.1},
        "solar": [{"kwp": 5.0}],
        "battery": {"size_kwh": 10.0, "inverter_kw": 5.0},
        "load": {"annual_kwh": 3800, "shape": "flat"},
        "tariff": {"rates_import": [{"rate": 28.0}]},
        "year": 2025,
    }
    if with_car:
        raw["load"]["car_charging_kwh"] = 2500
    return validate_config(raw, today=date(2026, 7, 25))


def run_one(my_predbat, config, weather, day):
    """Run all three scenarios for one day and return the results dict."""
    reset_inverter(my_predbat)
    midnight = pytz.utc.localize(datetime(day.year, day.month, day.day))
    load_source = SyntheticLoadProfile(annual_kwh=config["load"]["annual_kwh"], shape=config["load"]["shape"], year=config["year"])
    return run_day(my_predbat, config, weather, StubTariff(), load_source, day, midnight)


def _count_calculate_plan_calls(my_predbat, action):
    """Run ``action`` and return how many times it called ``calculate_plan``.

    run_day() plans a sampled day once when no car is configured and twice - a with-car
    leg and a without-car leg it blends against - when one is. Counting the calls is the
    only externally visible way to tell those two paths apart, since the blended result
    of two identical no-car legs would be indistinguishable from a single leg's.
    """
    calls = {"n": 0}
    original = my_predbat.calculate_plan

    def counting_calculate_plan(*args, **kwargs):
        """Count the call, then delegate to the real planner."""
        calls["n"] += 1
        return original(*args, **kwargs)

    my_predbat.calculate_plan = counting_calculate_plan
    try:
        action()
    finally:
        # my_predbat is the shared suite fixture, so the patch must not outlive this call
        del my_predbat.calculate_plan
    return calls["n"]


def test_annual_integration(my_predbat):
    """Verify scenario ordering, the forecast/actuals split, and state isolation."""
    failed = False
    print("**** Testing annual integration ****")

    config = make_config()
    weather = StubWeather(peak_kw=4.0)
    day = date(2025, 5, 15)

    print("Test: the three scenarios run and produce the expected keys")
    results = run_one(my_predbat, config, weather, day)
    for key in ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]:
        if key not in results:
            print("  ERROR: missing scenario '{}'".format(key))
            failed = True
            continue
        for field in ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh"]:
            if field not in results[key]:
                print("  ERROR: scenario '{}' is missing field '{}'".format(key, field))
                failed = True

    print("Test: predbat_cost <= without_predbat_cost <= no_pvbat_cost")
    if not failed:
        predbat_cost = results["with_predbat"]["cost_p"]
        baseline_cost = results["without_predbat"]["cost_p"]
        none_cost = results["no_pvbat"]["cost_p"]
        if not predbat_cost <= baseline_cost + 1e-6:
            print("  ERROR: Predbat cost {} should not exceed the dumb baseline {}".format(predbat_cost, baseline_cost))
            failed = True
        if not baseline_cost <= none_cost + 1e-6:
            print("  ERROR: the PV/battery baseline {} should not exceed the no-system cost {}".format(baseline_cost, none_cost))
            failed = True

    print("Test: the no-PV/no-battery scenario generates nothing and stores nothing")
    if results["no_pvbat"]["pv_generated_kwh"] != 0.0:
        print("  ERROR: the no-system scenario should generate no PV, got {}".format(results["no_pvbat"]["pv_generated_kwh"]))
        failed = True
    if results["no_pvbat"]["battery_throughput_kwh"] != 0.0:
        print("  ERROR: the no-system scenario should cycle no battery, got {}".format(results["no_pvbat"]["battery_throughput_kwh"]))
        failed = True

    print("Test: pv_only generates PV but stores none of it")
    pv_only = results["pv_only"]
    if pv_only["pv_generated_kwh"] <= 0:
        print("  ERROR: the PV-only scenario should generate PV, got {}".format(pv_only["pv_generated_kwh"]))
        failed = True
    if pv_only["battery_throughput_kwh"] != 0:
        print("  ERROR: the PV-only scenario must have no battery throughput, got {}".format(pv_only["battery_throughput_kwh"]))
        failed = True

    print("Test: pv_only sits between the no-system baseline and PV-plus-battery on cost")
    # Not an arbitrary ordering check: PV alone must beat no system (it displaces import),
    # and must not beat PV plus a battery (the battery can only add value). A violation
    # means the scenario is not modelling what its name says.
    if not (results["no_pvbat"]["cost_p"] > pv_only["cost_p"] > results["without_predbat"]["cost_p"]):
        print("  ERROR: expected no_pvbat > pv_only > without_predbat on cost, got {} / {} / {}".format(results["no_pvbat"]["cost_p"], pv_only["cost_p"], results["without_predbat"]["cost_p"]))
        failed = True

    print("Test: Predbat is billed on actuals, not on the forecast it planned against")
    # pv_generated_kwh is NOT proof the swap ran: _billed_result() reads it from the pv_step
    # ARGUMENT run_day() passes in, which is always actual_step regardless of the swap, so it
    # would still read correctly even if the swap back to actuals were deleted entirely - the
    # check on it below is a sanity check only. The decisive, non-noise-sensitive check is
    # structural: after run_day() returns, predbat.prediction is the exact object annual.py
    # builds from actual_step immediately before costing scenario 3 (the "swap"), and
    # run_prediction() (called from _billed_result) reads pv data from THIS object, not from
    # whatever calculate_plan() searched against. So predbat.prediction.pv_forecast_minute_step
    # must total the ACTUAL pv energy, not the (here, 3x inflated) forecast calculate_plan()
    # was given - a plan-search-noise-proof check, since actual and 3x-inflated-forecast
    # totals differ by a factor of three, not a fraction of a percent.
    midnight = pytz.utc.localize(datetime(day.year, day.month, day.day))
    actual_pv_total = sum(weather.pv_minutes("actual", midnight, PLAN_MINUTES).values())
    honest_prediction_pv_total = sum(my_predbat.prediction.pv_forecast_minute_step.values())
    if abs(honest_prediction_pv_total - actual_pv_total) > 0.5:
        print("  ERROR: after an honest-forecast run, predbat.prediction.pv_forecast_minute_step should total the actual PV energy ({}), got {}".format(actual_pv_total, honest_prediction_pv_total))
        failed = True

    inflated = StubWeather(peak_kw=4.0, forecast_multiplier=3.0)
    inflated_forecast_total = sum(inflated.pv_minutes("forecast", midnight, PLAN_MINUTES).values())
    inflated_results = run_one(my_predbat, config, inflated, day)
    inflated_prediction_pv_total = sum(my_predbat.prediction.pv_forecast_minute_step.values())
    if abs(inflated_prediction_pv_total - actual_pv_total) > 0.5:
        print("  ERROR: with a 3x inflated forecast, pv_forecast_minute_step should still total the actual PV energy ({}), got {} (forecast alone totals {})".format(actual_pv_total, inflated_prediction_pv_total, inflated_forecast_total))
        failed = True
    if abs(inflated_prediction_pv_total - inflated_forecast_total) < 1.0:
        print("  ERROR: predbat.prediction.pv_forecast_minute_step matches the inflated FORECAST total ({}) rather than actuals ({}) - the swap back to actuals did not happen".format(inflated_forecast_total, actual_pv_total))
        failed = True

    honest_pv = results["with_predbat"]["pv_generated_kwh"]
    inflated_pv = inflated_results["with_predbat"]["pv_generated_kwh"]
    if abs(inflated_pv - honest_pv) > 0.01:
        print("  ERROR: reported PV should track actuals ({}) regardless of the forecast, got {}".format(honest_pv, inflated_pv))
        failed = True

    print("Test: state isolation - a day run in isolation matches the same day run after another")
    isolated = run_one(my_predbat, config, weather, day)
    _ = run_one(my_predbat, config, StubWeather(peak_kw=1.0), date(2025, 11, 20))
    after_other = run_one(my_predbat, config, weather, day)
    for scenario in ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]:
        for field in ["cost_p", "import_kwh", "export_kwh", "battery_throughput_kwh"]:
            first = isolated[scenario][field]
            second = after_other[scenario][field]
            if abs(first - second) > 1e-6:
                print("  ERROR: {}.{} changed from {} to {} depending on what ran before it".format(scenario, field, first, second))
                failed = True

    print("Test: a config with no car plans a single leg (one calculate_plan call), not two")
    # Guards run_day()'s dispatch: a config with no car must take the cheap, single-leg path
    # rather than always running both the with-car and without-car legs.
    plan_calls = _count_calculate_plan_calls(my_predbat, lambda: run_one(my_predbat, config, weather, day))
    if plan_calls != 1:
        print("  ERROR: a config with no car should call calculate_plan exactly once, got {}".format(plan_calls))
        failed = True

    print("Test: a car charging config plans TWICE (a with-car leg and a without-car leg) and blends the two")
    car_config = make_config(with_car=True)
    car_results = {}

    def _run_car_config():
        """Run the car-charging config for one day and record its blended result."""
        car_results.update(run_one(my_predbat, car_config, weather, day))

    plan_calls = _count_calculate_plan_calls(my_predbat, _run_car_config)
    if plan_calls != 2:
        print("  ERROR: a config with a car should call calculate_plan exactly twice (with-car and without-car legs), got {}".format(plan_calls))
        failed = True

    print("Test: a car charging config still produces an ordered result")
    if car_results["with_predbat"]["cost_p"] > car_results["without_predbat"]["cost_p"] + 1e-6:
        print("  ERROR: with a car, Predbat cost {} should not exceed the timer baseline {}".format(car_results["with_predbat"]["cost_p"], car_results["without_predbat"]["cost_p"]))
        failed = True
    if car_results["no_pvbat"]["import_kwh"] <= results["no_pvbat"]["import_kwh"]:
        print("  ERROR: adding a car should raise the no-system import, got {} vs {}".format(car_results["no_pvbat"]["import_kwh"], results["no_pvbat"]["import_kwh"]))
        failed = True

    print("Test: the blended result equals f * with-car leg + (1 - f) * standalone no-car leg")
    # What this covers: run_day()'s WIRING - that it feeds the two legs into the blend in
    # the right order, with the right fraction, and mutates nothing else along the way.
    # The expected value below is computed with explicit arithmetic rather than by calling
    # _blend_results(), so a bug inside _blend_results() cannot cancel itself out across
    # both sides of the comparison.
    #
    # What it does NOT cover, despite the tempting reading: leg contamination. The manual
    # replay below runs the same two legs through the same code path in the same order as
    # run_day() does, so a reset_sample_state() regression would corrupt both sides
    # identically and stay inside tolerance. Regression cover for the car-field reset is
    # the state-based assertion in test_annual_bootstrap.py; blend arithmetic on fabricated
    # inputs is covered in test_annual_scenarios.py.
    car_annual_kwh = car_config["load"]["car_charging_kwh"]
    car_rate_kw = car_config["load"]["car_rate_kw"]
    sessions_per_week, session_kwh = car_charging_schedule(car_annual_kwh, car_rate_kw)
    fraction = sessions_per_week / float(MAX_SESSIONS_PER_WEEK)

    reset_inverter(my_predbat)
    midnight = pytz.utc.localize(datetime(day.year, day.month, day.day))
    car_load_source = SyntheticLoadProfile(annual_kwh=car_config["load"]["annual_kwh"], shape=car_config["load"]["shape"], year=car_config["year"])
    with_car_leg = _run_scenarios(my_predbat, car_config, weather, StubTariff(), car_load_source, day, midnight, car_kwh=session_kwh, car_rate_kw=car_rate_kw)
    standalone_no_car_leg = _run_scenarios(my_predbat, car_config, weather, StubTariff(), car_load_source, day, midnight, car_kwh=0.0, car_rate_kw=car_rate_kw)

    run_day_blend = run_one(my_predbat, car_config, weather, day)
    for key in SCENARIO_KEYS:
        for field in SCENARIO_FIELDS:
            expected_value = fraction * with_car_leg[key][field] + (1.0 - fraction) * standalone_no_car_leg[key][field]
            actual_value = run_day_blend[key][field]
            if abs(expected_value - actual_value) > 1e-6:
                print("  ERROR: blended {}.{} = {}, expected f * with_car_leg + (1 - f) * standalone_no_car_leg = {}".format(key, field, actual_value, expected_value))
                failed = True

    print("Test: capturing plans does not change the billed figures (save leaking into the numbers)")
    # _billed_result() threads save="best" into the SAME run_prediction() call the annual
    # engine already makes when a scenario's plan is captured, rather than running a second
    # prediction. save only gates copying predict_*_best onto predbat, logging and dashboard
    # writes - all of which happen AFTER the billed tuple has already been computed and
    # returned - so it must never change the figures a scenario is billed for. This proves
    # that by running the identical scenario twice back to back and diffing every field,
    # rather than assuming save is side-effect-free on the numbers.
    reset_inverter(my_predbat)
    capture_midnight = pytz.utc.localize(datetime(day.year, day.month, day.day))
    capture_load_source = SyntheticLoadProfile(annual_kwh=config["load"]["annual_kwh"], shape=config["load"]["shape"], year=config["year"])
    without_capture = _run_scenarios(my_predbat, config, weather, StubTariff(), capture_load_source, day, capture_midnight, car_kwh=0.0, car_rate_kw=DEFAULT_CAR_RATE_KW)
    reset_inverter(my_predbat)
    plans = {}
    with_capture = _run_scenarios(my_predbat, config, weather, StubTariff(), capture_load_source, day, capture_midnight, car_kwh=0.0, car_rate_kw=DEFAULT_CAR_RATE_KW, plans=plans)
    for key in SCENARIO_KEYS:
        for field in SCENARIO_FIELDS:
            if without_capture[key][field] != with_capture[key][field]:
                print("  ERROR: {}.{} = {} without capture but {} with capture - save is leaking into the billed numbers".format(key, field, without_capture[key][field], with_capture[key][field]))
                failed = True

    print("Test: the baseline tariff prices no_pvbat only and does not leak into the other scenarios")
    # A flat price-cap-style baseline against the banded main tariff. Two properties
    # matter and they pull in opposite directions: the counterfactual MUST change (or the
    # feature does nothing), and every other scenario must be bit-for-bit identical (or
    # the swap has leaked and silently repriced the system being evaluated). _apply_rates
    # mutates predbat in place and replicates what it is given, so a leak here is a very
    # live possibility rather than a theoretical one.
    reset_inverter(my_predbat)
    baseline_midnight = pytz.utc.localize(datetime(day.year, day.month, day.day))
    baseline_load_source = SyntheticLoadProfile(annual_kwh=config["load"]["annual_kwh"], shape=config["load"]["shape"], year=config["year"])
    flat_cap = StubTariff(cheap=26.11, normal=26.11, peak=26.11, export=4.1)
    without_baseline = _run_scenarios(my_predbat, config, weather, StubTariff(), baseline_load_source, day, baseline_midnight, car_kwh=0.0, car_rate_kw=DEFAULT_CAR_RATE_KW)
    reset_inverter(my_predbat)
    with_baseline = _run_scenarios(my_predbat, config, weather, StubTariff(), baseline_load_source, day, baseline_midnight, car_kwh=0.0, car_rate_kw=DEFAULT_CAR_RATE_KW, baseline_tariff=flat_cap)

    if abs(with_baseline["no_pvbat"]["cost_p"] - without_baseline["no_pvbat"]["cost_p"]) < 1.0:
        print("  ERROR: a flat baseline tariff should reprice no_pvbat away from the banded main tariff, got {} vs {}".format(without_baseline["no_pvbat"]["cost_p"], with_baseline["no_pvbat"]["cost_p"]))
        failed = True
    for key in ["pv_only", "without_predbat", "with_predbat"]:
        for field in SCENARIO_FIELDS:
            if without_baseline[key][field] != with_baseline[key][field]:
                print("  ERROR: {}.{} changed from {} to {} - the baseline tariff has leaked past no_pvbat".format(key, field, without_baseline[key][field], with_baseline[key][field]))
                failed = True

    print("Test: capturing plans does not leak predbat.debug_enable on")
    # The annual "debug" flag means "save the plan info", nothing more - it must never be
    # wired to Predbat's own debug_enable, which kernel_supported() requires False to use
    # the fast C++ prediction kernel. Leaving debug_enable True on the shared fixture has
    # already caused one apparent hang (an 8x slowdown from every later plan falling back
    # to pure Python), so this pins it off after a capturing run.
    if my_predbat.debug_enable is not False:
        print("  ERROR: predbat.debug_enable should still be False after a capturing run, got {!r}".format(my_predbat.debug_enable))
        failed = True

    print("Test: a scenario's captured plan reflects ITS OWN state, not stale state from another scenario")
    # no_pvbat runs with soc_max=0 (see _run_scenarios()), so every row of ITS captured plan
    # must show 0% SoC. If capture were reading leftover state from a battery scenario -
    # for example because predbat.prediction or the *_best arrays were not refreshed before
    # the capture - this would be non-zero, which is what makes the check discriminating.
    if any(row.get("soc_percent", 0) != 0 for row in plans["no_pvbat"]["rows"]):
        print("  ERROR: no_pvbat's captured rows should all show 0% SoC (soc_max=0), got at least one non-zero soc_percent")
        failed = True
    for key in SCENARIO_KEYS:
        if not plans[key]["rows"]:
            print("  ERROR: {}'s captured plan has no rows - the viewer would render 'No plan data available'".format(key))
            failed = True

    return failed
