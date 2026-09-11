# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for set_rate_thresholds and rate_minmax_excluding_saving (GH#5050).

A saving session / Axle VPP event boosts both the import and export rate tables by the event
reward and tags those minutes "saving" in rate_import_replicated/rate_export_replicated
(load_saving_slot() in octopus.py, load_axle_slot() in axle.py). In automatic threshold mode
(rate_low_threshold=0) set_rate_thresholds() used to compute rate_import_cost_threshold from
self.rate_max, which includes those event-boosted minutes - so a two-rate tariff (25.95p day,
3.49p night) plus a +100p event pushed the threshold to 125.45p, and rate_scan_window()
classified the whole ordinary-price day as "low rate" (binary_sensor.predbat_low_rate_slot stuck
ON for ~24h, the reported symptom).

set_rate_thresholds() now derives its threshold stats from rate_minmax_excluding_saving(), which
scans the same rates but skips any minute tagged "saving". self.rate_max/rate_min/rate_average
(and the export equivalents) are deliberately left untouched - they feed dashboard sensors, graph
scaling, and plan.py pricing, where the real boosted price is what should be shown.
"""


def _setup_two_rate_tariff(my_predbat, event_start, event_end, event_boost=100.0):
    """Build a 48h two-rate import tariff (25.95p day 06:00-22:00, 3.49p night) plus a flat 15.0p
    export tariff, with a saving-session-style boost applied to import over [event_start, event_end)
    and tagged "saving" in rate_import_replicated - the shape load_saving_slot()/load_axle_slot()
    produce."""
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60

    rate_import = {}
    for minute in range(0, 48 * 60):
        hour = (minute // 60) % 24
        rate_import[minute] = 25.95 if 6 <= hour < 22 else 3.49

    rate_import_replicated = {}
    for minute in range(event_start, event_end):
        rate_import[minute] += event_boost
        rate_import_replicated[minute] = "saving"

    rate_export = {minute: 15.0 for minute in range(0, 48 * 60)}

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_replicated = rate_import_replicated
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_replicated = {}

    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)

    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    return rate_import, rate_import_replicated


def test_rate_minmax_excluding_saving_skips_the_boosted_minutes(my_predbat):
    """rate_minmax_excluding_saving must report the tariff's own min/max/average, not the
    event-inflated figures - reproducing the exact numbers from the GH#5050 triage."""
    print("**** test_rate_minmax_excluding_saving_skips_the_boosted_minutes ****")
    failed = False

    rate_import, rate_import_replicated = _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60)

    if my_predbat.rate_max != 125.95:
        print("ERROR: test setup sanity check failed - contaminated rate_max should be 125.95, got {}".format(my_predbat.rate_max))
        failed = True

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_replicated)
    if rate_min != 3.49:
        print("ERROR: saving-excluded rate_min should be 3.49, got {}".format(rate_min))
        failed = True
    if rate_max != 25.95:
        print("ERROR: saving-excluded rate_max should be 25.95 (the true day rate), got {}".format(rate_max))
        failed = True
    if abs(rate_average - 17.78) > 0.01:
        print("ERROR: saving-excluded rate_average should be ~17.78, got {}".format(rate_average))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_rate_minmax_excluding_saving_falls_back_when_everything_is_tagged(my_predbat):
    """If an event covers the whole forecast window there is no genuine tariff minute to scan -
    fall back to the plain min/max/average rather than returning a 99999/0/0 that would make every
    downstream comparison in set_rate_thresholds() behave as if there were no data at all."""
    print("**** test_rate_minmax_excluding_saving_falls_back_when_everything_is_tagged ****")
    failed = False

    rate_import, rate_import_replicated = _setup_two_rate_tariff(my_predbat, event_start=0, event_end=my_predbat.forecast_minutes)

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_replicated)
    expected_min, expected_max, expected_average, _, _ = my_predbat.rate_minmax(rate_import)
    if (rate_min, rate_max, rate_average) != (expected_min, expected_max, expected_average):
        print("ERROR: fully-tagged window should fall back to the plain scan {}, got {}".format((expected_min, expected_max, expected_average), (rate_min, rate_max, rate_average)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode(my_predbat):
    """End-to-end: with rate_low_threshold=0 (automatic mode), a saving-session boost must not
    raise the low-rate threshold above the tariff's own day rate - the exact mechanism reported in
    GH#5050 (binary_sensor.predbat_low_rate_slot stuck ON through the day rate for ~24h)."""
    print("**** test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode ****")
    failed = False

    _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60)

    my_predbat.set_rate_thresholds()

    # rate_max - 0.5 on the true day rate, not the event-boosted one: 25.95 - 0.5 = 25.45.
    # The unfixed code produced rate_max(125.95) - 0.5 = 125.45, which classified the whole day as low.
    expected_threshold = 25.45
    if abs(my_predbat.rate_import_cost_threshold - expected_threshold) > 0.01:
        print("ERROR: rate_import_cost_threshold should be {} (true day rate - 0.5), got {} (GH#5050)".format(expected_threshold, my_predbat.rate_import_cost_threshold))
        failed = True

    found, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)
    window_averages = sorted(set(window["average"] for window in found))
    if 25.95 in window_averages:
        print("ERROR: the 25.95p day rate was classified as a low-rate window - binary_sensor.predbat_low_rate_slot would misfire (GH#5050)".format())
        failed = True
    if window_averages != [3.49]:
        print("ERROR: expected only the genuine 3.49p night rate to qualify as low-rate, got window averages {}".format(window_averages))
        failed = True

    # rate_max/rate_min/rate_average themselves are deliberately left contaminated - other code
    # (dashboard sensors, graph scaling, plan.py pricing) wants the real boosted price.
    if my_predbat.rate_max != 125.95:
        print("ERROR: rate_max should be left untouched at 125.95 for downstream consumers, got {}".format(my_predbat.rate_max))
        failed = True

    if not failed:
        print("PASS")
    return failed


def run_set_rate_thresholds_tests(my_predbat):
    """Run the set_rate_thresholds / rate_minmax_excluding_saving tests"""
    failed = False
    failed |= test_rate_minmax_excluding_saving_skips_the_boosted_minutes(my_predbat)
    failed |= test_rate_minmax_excluding_saving_falls_back_when_everything_is_tagged(my_predbat)
    failed |= test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode(my_predbat)
    return failed
