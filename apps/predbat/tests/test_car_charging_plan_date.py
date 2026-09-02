# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the multi-day car_charging_plan_date dropdown and plan engine."""
from datetime import timedelta

from tests.test_infra import reset_inverter, reset_rates2


def _format_date(my_predbat, day_offset):
    """Format ``midnight_utc + day_offset`` using the userinterface CAR_PLAN_DATE_FORMAT."""
    target = (my_predbat.midnight_utc + timedelta(days=day_offset)).date()
    return target.strftime(my_predbat.CAR_PLAN_DATE_FORMAT)


def _setup_single_car(my_predbat, plan_time="07:00:00", plan_date="Default"):
    """Configure a single-car plan_car_charging scenario with shared defaults."""
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.car_charging_limit = [100.0]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_rate = [10.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_plan_max_price = [99]
    my_predbat.car_charging_plan_smart = [True]
    my_predbat.car_charging_plan_time = [plan_time]
    my_predbat.car_charging_plan_date = [plan_date]
    my_predbat.car_charging_now = [False]
    my_predbat.num_cars = 1


def _test_default_preserves_wrap(my_predbat):
    """Default sentinel preserves the existing 24-hour wrap behaviour."""
    failed = False
    print("**** Running Test: plan_date_default_preserves_wrap ****")
    _setup_single_car(my_predbat, plan_time="07:00:00", plan_date="Default")
    slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    if not slots:
        print("ERROR: Default plan_date should produce charging slots within 24h")
        failed = True
    return failed


def _test_future_date_extends_window(my_predbat):
    """A plan_date one day in the future doubles the planning window for the car."""
    failed = False
    print("**** Running Test: plan_date_future_extends_window ****")
    _setup_single_car(my_predbat, plan_time="07:00:00", plan_date="Default")
    default_slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    default_kwh = sum(slot["kwh"] for slot in default_slots)

    _setup_single_car(my_predbat, plan_time="07:00:00", plan_date=_format_date(my_predbat, 1))
    future_slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    future_kwh = sum(slot["kwh"] for slot in future_slots)

    if future_kwh < default_kwh:
        print("ERROR: Future plan_date should not reduce charging energy ({} < {})".format(future_kwh, default_kwh))
        failed = True
    return failed


def _test_today_date_falls_through(my_predbat):
    """A plan_date of today falls through to the existing wrap (treated as Default)."""
    failed = False
    print("**** Running Test: plan_date_today_falls_through ****")
    _setup_single_car(my_predbat, plan_time="07:00:00", plan_date=_format_date(my_predbat, 0))
    today_slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)

    _setup_single_car(my_predbat, plan_time="07:00:00", plan_date="Default")
    default_slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)

    today_kwh = sum(slot["kwh"] for slot in today_slots)
    default_kwh = sum(slot["kwh"] for slot in default_slots)
    if today_kwh != default_kwh:
        print("ERROR: Today plan_date should match Default behaviour ({} != {})".format(today_kwh, default_kwh))
        failed = True
    return failed


def _test_invalid_date_falls_through(my_predbat):
    """A malformed plan_date string is treated as Default rather than raising."""
    failed = False
    print("**** Running Test: plan_date_invalid_falls_through ****")
    _setup_single_car(my_predbat, plan_time="07:00:00", plan_date="not a date")
    parsed = my_predbat.parse_car_plan_date("not a date")
    if parsed is not None:
        print("ERROR: Invalid plan_date should parse as None, got {}".format(parsed))
        failed = True
    slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    if not slots:
        print("ERROR: Invalid plan_date should still produce charging slots via fallback")
        failed = True
    return failed


def _test_stale_past_date_resets_to_default(my_predbat):
    """A previously-selected date that has now passed is reset to Default in the dropdown."""
    failed = False
    print("**** Running Test: plan_date_stale_past_date_resets_to_default ****")
    item = my_predbat.config_index.get("car_charging_plan_date")
    if item is None:
        print("ERROR: car_charging_plan_date config item missing")
        return True

    # Simulate a stored value from before today (parses cleanly but is in the past).
    yesterday = (my_predbat.midnight_utc - timedelta(days=2)).date()
    stale = yesterday.strftime(my_predbat.CAR_PLAN_DATE_FORMAT)
    item["value"] = stale

    my_predbat.forecast_plan_hours = 96
    my_predbat.num_cars = 1
    my_predbat.car_plan_date_options()

    if item["value"] != my_predbat.CAR_PLAN_DATE_DEFAULT:
        print("ERROR: Stale past date should reset to Default, got {}".format(item["value"]))
        failed = True
    if stale in item["options"]:
        print("ERROR: Stale past date should be removed from options after reset, but {} still present".format(stale))
        failed = True
    return failed


def _test_options_helper_respects_horizon(my_predbat):
    """car_plan_date_options caps the dropdown at min(forecast_plan_hours, 96)//24 days."""
    failed = False
    print("**** Running Test: plan_date_options_respect_horizon ****")
    item = my_predbat.config_index.get("car_charging_plan_date")
    if item is None:
        print("ERROR: car_charging_plan_date config item missing")
        return True

    my_predbat.forecast_plan_hours = 24
    my_predbat.num_cars = 1
    my_predbat.car_plan_date_options()
    options_24h = list(item["options"])

    my_predbat.forecast_plan_hours = 96
    my_predbat.car_plan_date_options()
    options_96h = list(item["options"])

    if "Default" not in options_24h or "Default" not in options_96h:
        print("ERROR: Default sentinel must always be present in options")
        failed = True
    if len(options_96h) <= len(options_24h):
        print("ERROR: 96h horizon should expose more date options than 24h ({} <= {})".format(len(options_96h), len(options_24h)))
        failed = True
    return failed


def _test_parse_round_trips(my_predbat):
    """Formatted dates round-trip through parse_car_plan_date back to a date."""
    failed = False
    print("**** Running Test: plan_date_parse_round_trips ****")
    for offset in range(0, 4):
        formatted = _format_date(my_predbat, offset)
        parsed = my_predbat.parse_car_plan_date(formatted)
        expected = (my_predbat.midnight_utc + timedelta(days=offset)).date()
        if parsed != expected:
            print("ERROR: round-trip failed for offset {} ({} -> {} != {})".format(offset, formatted, parsed, expected))
            failed = True

    if my_predbat.parse_car_plan_date("Default") is not None:
        print("ERROR: Default sentinel must parse as None")
        failed = True
    if my_predbat.parse_car_plan_date("") is not None:
        print("ERROR: Empty string must parse as None")
        failed = True
    return failed


def run_car_charging_plan_date_tests(my_predbat):
    """Run the full car_charging_plan_date test suite."""
    failed = False
    reset_inverter(my_predbat)

    print("**** Running Car Charging Plan Date tests ****")
    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)
    my_predbat.low_rates, _, _ = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)

    failed |= _test_parse_round_trips(my_predbat)
    failed |= _test_default_preserves_wrap(my_predbat)
    failed |= _test_today_date_falls_through(my_predbat)
    failed |= _test_invalid_date_falls_through(my_predbat)
    failed |= _test_future_date_extends_window(my_predbat)
    failed |= _test_options_helper_respects_horizon(my_predbat)
    failed |= _test_stale_past_date_resets_to_default(my_predbat)

    return failed
