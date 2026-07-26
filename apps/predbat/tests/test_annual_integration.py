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

from annual import DAY_MINUTES, run_day, validate_config
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


def test_annual_integration(my_predbat):
    """Verify scenario ordering, the forecast/actuals split, and state isolation."""
    failed = False
    print("**** Testing annual integration ****")

    config = make_config()
    weather = StubWeather(peak_kw=4.0)
    day = date(2025, 5, 15)

    print("Test: the three scenarios run and produce the expected keys")
    results = run_one(my_predbat, config, weather, day)
    for key in ["no_pvbat", "without_predbat", "with_predbat"]:
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

    print("Test: Predbat is billed on actuals, not on the forecast it planned against")
    # pv_generated_kwh is derived from the ACTUAL pv_step regardless of scenario
    # (_billed_result's pv_step argument in annual.py always comes from actual_step), so it is
    # identical between the honest and inflated runs by construction and does NOT itself
    # exercise the Prediction swap - the assertion below on it is a sanity check, not proof.
    # What does exercise the swap is cost: with the swap in place, calculate_plan() commits to
    # the inflated forecast's phantom solar, then run_prediction() bills against the actuals
    # that never delivered it, so the inflated run's billed cost should be MATERIALLY worse,
    # not merely "not cheaper". Observed on this fixture: honest with_predbat cost_p is
    # -471.6792p, 3x-inflated-forecast cost_p is -461.2486p, a ~10.43p degradation;
    # material_degradation_p is set below that with margin so the assertion is not a knife edge.
    inflated = StubWeather(peak_kw=4.0, forecast_multiplier=3.0)
    inflated_results = run_one(my_predbat, config, inflated, day)
    honest_pv = results["with_predbat"]["pv_generated_kwh"]
    inflated_pv = inflated_results["with_predbat"]["pv_generated_kwh"]
    if abs(inflated_pv - honest_pv) > 0.01:
        print("  ERROR: reported PV should track actuals ({}) regardless of the forecast, got {}".format(honest_pv, inflated_pv))
        failed = True
    honest_cost = results["with_predbat"]["cost_p"]
    inflated_cost = inflated_results["with_predbat"]["cost_p"]
    material_degradation_p = 3.0
    if inflated_cost < honest_cost - 1e-6:
        print("  ERROR: planning against an over-optimistic forecast must not make the billed cost cheaper")
        failed = True
    if inflated_cost < honest_cost + material_degradation_p:
        print("  ERROR: an over-optimistic forecast should cost at least {}p more (over-commits to solar that never arrives); got honest {} vs inflated {}".format(material_degradation_p, honest_cost, inflated_cost))
        failed = True

    print("Test: state isolation - a day run in isolation matches the same day run after another")
    isolated = run_one(my_predbat, config, weather, day)
    _ = run_one(my_predbat, config, StubWeather(peak_kw=1.0), date(2025, 11, 20))
    after_other = run_one(my_predbat, config, weather, day)
    for scenario in ["no_pvbat", "without_predbat", "with_predbat"]:
        for field in ["cost_p", "import_kwh", "export_kwh", "battery_throughput_kwh"]:
            first = isolated[scenario][field]
            second = after_other[scenario][field]
            if abs(first - second) > 1e-6:
                print("  ERROR: {}.{} changed from {} to {} depending on what ran before it".format(scenario, field, first, second))
                failed = True

    print("Test: a car charging config still produces an ordered result")
    car_config = make_config(with_car=True)
    car_results = run_one(my_predbat, car_config, weather, day)
    if car_results["with_predbat"]["cost_p"] > car_results["without_predbat"]["cost_p"] + 1e-6:
        print("  ERROR: with a car, Predbat cost {} should not exceed the timer baseline {}".format(car_results["with_predbat"]["cost_p"], car_results["without_predbat"]["cost_p"]))
        failed = True
    if car_results["no_pvbat"]["import_kwh"] <= results["no_pvbat"]["import_kwh"]:
        print("  ERROR: adding a car should raise the no-system import, got {} vs {}".format(car_results["no_pvbat"]["import_kwh"], results["no_pvbat"]["import_kwh"]))
        failed = True

    return failed
