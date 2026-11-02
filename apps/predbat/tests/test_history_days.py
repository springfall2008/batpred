# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for the History view replay (Output.replay_history_day) and the 7-day History.

The History view is a replay of what was recorded (SoC, cost, status), rendered through the
plan table, not a simulation. calculate_yesterday() builds yesterday + today from it, and the
older days in the picker are built from the same replay, one day at a time.
"""

from datetime import datetime, timedelta, timezone

from output import Output
from tests.test_calculate_yesterday import _setup_base, _apply_mocks, _restore_methods, _make_constant_history


def _test_replay_called_by_calculate_yesterday(my_predbat, failed):
    """calculate_yesterday() builds the History view through replay_history_day() and publishes what it returns."""
    print("history_days: replay_history_day builds the yesterday + today History view")
    now_utc = _setup_base(my_predbat, minutes_now=360)
    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)
    published_rows = [{"time": "Thu 00:00", "slot_minute": 0, "cost_change": 0.12}]
    my_predbat.publish_html_plan = lambda *a, **kw: ("<table></table>", {"rows": list(published_rows)})

    calls = []
    if not hasattr(Output, "replay_history_day"):
        print("ERROR: Output.replay_history_day does not exist")
        _restore_methods(my_predbat, original_run_pred)
        my_predbat.savings_last_updated = None
        return True

    def _spy(*args, **kwargs):
        """Record the call and pass it through to the real replay."""
        result = Output.replay_history_day(my_predbat, *args, **kwargs)
        calls.append((args, kwargs, result))
        return result

    my_predbat.replay_history_day = _spy
    try:
        my_predbat.calculate_yesterday()
    finally:
        del my_predbat.replay_history_day
        _restore_methods(my_predbat, original_run_pred)
        my_predbat.savings_last_updated = None

    if len(calls) != 1:
        print("ERROR: expected replay_history_day to be called once, got {}".format(len(calls)))
        return True
    args, kwargs, result = calls[0]
    view_minutes = kwargs.get("view_minutes", args[0] if args else None)
    status_offset = kwargs.get("status_offset", args[1] if len(args) > 1 else None)
    if view_minutes != 24 * 60 + 360 or status_offset != 24 * 60 + 360:
        print("ERROR: yesterday replay should cover 24h + minutes_now with the matching status offset, got view_minutes {} status_offset {}".format(view_minutes, status_offset))
        failed = True
    plan_html, plan_json = result
    published = my_predbat.get_state_wrapper(my_predbat.prefix + ".cost_yesterday", attribute="json")
    if not isinstance(published, dict) or published.get("rows") != published_rows or published is not plan_json:
        print("ERROR: cost_yesterday json should be the plan JSON replay_history_day returned, got {}".format(published))
        failed = True
    if plan_json.get("refresh_minutes") != my_predbat.history_refresh_minutes():
        print("ERROR: replay JSON should carry refresh_minutes {}, got {}".format(my_predbat.history_refresh_minutes(), plan_json.get("refresh_minutes")))
        failed = True
    return failed


def _history_mock(my_predbat, now_utc, missing=(), days=9, rate=25.0):
    """get_history_wrapper replacement: constant histories for every entity the History view reads."""
    prefix = my_predbat.prefix
    values = {".cost_today": 100.0, ".cost_today_import": 80.0, ".soc_kw_h0": 5.0, ".rates": rate, ".rates_export": 5.0}

    def _get_history_wrapper(entity_id, days=30, required=True, tracked=True):
        """Return a constant history, or None for the entities listed as missing."""
        for suffix, value in values.items():
            if entity_id == prefix + suffix:
                return None if suffix in missing else _make_constant_history(value, now_utc, days=days)
        return None

    return _get_history_wrapper


def _spy_replay(my_predbat, calls):
    """Replace replay_history_day with a recorder that captures the rates it was run with."""

    def _replay(*args, **kwargs):
        """Record the call, the faked import rate at minute 0 and the midnight it ran at."""
        calls.append({"kwargs": kwargs, "rate0": my_predbat.rate_import.get(0), "midnight": my_predbat.midnight_utc})
        return "", {"rows": [{"slot_minute": 600, "cost_change": 0.0}]}

    my_predbat.replay_history_day = _replay


def _prepare_history_days(my_predbat, max_days_previous=8, missing=()):
    """Base state + mocks for calculate_history_days; returns (now_utc, calls, original_run_pred)."""
    now_utc = _setup_base(my_predbat, minutes_now=360)
    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)
    my_predbat.get_history_wrapper = _history_mock(my_predbat, now_utc, missing=missing)
    my_predbat.max_days_previous = max_days_previous
    my_predbat.history_days_built_for = None
    my_predbat.history_days_billing_mark = None
    calls = []
    _spy_replay(my_predbat, calls)
    return now_utc, calls, original_run_pred


def _cleanup_history_days(my_predbat, original_run_pred):
    """Undo _prepare_history_days."""
    for name in ("replay_history_day",):
        if name in my_predbat.__dict__:
            del my_predbat.__dict__[name]
    _restore_methods(my_predbat, original_run_pred)
    my_predbat.savings_last_updated = None


def _test_history_days_built_once_per_day(my_predbat, failed):
    """The six older days are built once per local date, not on every run."""
    print("history_days: older days built once per day")
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat)
    try:
        my_predbat.calculate_history_days()
        first = len(calls)
        my_predbat.calculate_history_days()
        second = len(calls) - first
        my_predbat.now_utc = now_utc + timedelta(days=1)
        my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=1)
        my_predbat.calculate_history_days()
        third = len(calls) - first - second
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
    if first != 6 or second != 0 or third != 6:
        print("ERROR: expected 6 replays, then none on the same day, then 6 the next day; got {}, {}, {}".format(first, second, third))
        failed = True
    return failed


def _test_history_days_rebuild_on_new_billing(my_predbat, failed):
    """New billing arriving the same day rebuilds the older days so it shows straight away."""
    print("history_days: rebuilt when billing moves on")
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat)
    try:
        my_predbat.dashboard_item(
            my_predbat.prefix + ".billing",
            state=1.0,
            attributes={"complete_until": "2024-10-02T00:00:00Z", "slots": {"import": {"2024-10-01T10:00:00Z": {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}}}, "legs": {"import": True, "export": None}, "supplier": "octopus"},
        )
        my_predbat.calculate_history_days()
        first = len(calls)
        my_predbat.dashboard_item(
            my_predbat.prefix + ".billing",
            state=1.0,
            attributes={"complete_until": "2024-10-03T00:00:00Z", "slots": {"import": {"2024-10-01T10:00:00Z": {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}}}, "legs": {"import": True, "export": None}, "supplier": "octopus"},
        )
        my_predbat.calculate_history_days()
        second = len(calls) - first
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
        my_predbat.dashboard_item(my_predbat.prefix + ".billing", state=None, attributes={})
    if first != 6 or second != 6:
        print("ERROR: a new complete_until should rebuild the older days, got {} then {}".format(first, second))
        failed = True
    return failed


def _test_history_days_limited_by_history(my_predbat, failed):
    """Only days the loaded history reaches are built and published."""
    print("history_days: limited by max_days_previous")
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat, max_days_previous=4)
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
    if sorted(days) != ["2024-10-01", "2024-10-02"] or len(calls) != 2:
        print("ERROR: with max_days_previous 4 only 2 and 3 days back should be published, got {} ({} replays)".format(sorted(days), len(calls)))
        failed = True
    return failed


def _test_history_days_rates_from_history(my_predbat, failed):
    """Older days take their rates from the recorded predbat.rates history, and are left out without it."""
    print("history_days: rates from predbat.rates history")
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat)
    try:
        my_predbat.calculate_history_days()
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
    if not calls or any(call["rate0"] != 25.0 for call in calls):
        print("ERROR: each older day should be replayed with the recorded 25p rate, got {}".format([call["rate0"] for call in calls]))
        failed = True
    expected_midnight = my_predbat.local_tz.localize(datetime(2024, 10, 2)).astimezone(timezone.utc)
    if calls and calls[0]["midnight"] != expected_midnight:
        print("ERROR: the first older day should replay at local midnight on 2 Oct ({}), got {}".format(expected_midnight, calls[0]["midnight"]))
        failed = True
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat, missing=(".rates",))
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
    if calls or days:
        print("ERROR: without predbat.rates history no older day should be built, got {} replays, days {}".format(len(calls), sorted(days)))
        failed = True
    return failed


def _yesterday_billing(my_predbat, legs):
    """Publish a predbat.billing covering yesterday 10:00-10:30 UTC (the _setup_base day)."""
    slot = {"kwh": 1.0, "cost": 10.0, "standing": 1.0, "buckets": {"Home Low Rate": {"kwh": 1.0, "cost": 10.0}}}
    my_predbat.dashboard_item(my_predbat.prefix + ".billing", state=10.0, attributes={"complete_until": "2024-10-03T10:30:00Z", "slots": {"import": {"2024-10-03T10:00:00Z": slot}}, "legs": legs, "supplier": "octopus"})


def _run_yesterday_with_rows(my_predbat, missing=()):
    """Run calculate_yesterday with a renderer that returns one row at 10:00 and return the published json."""
    now_utc = _setup_base(my_predbat, minutes_now=360)
    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)
    my_predbat.get_history_wrapper = _history_mock(my_predbat, now_utc, missing=missing)
    my_predbat.plan_interval_minutes = 30
    my_predbat.publish_html_plan = lambda *a, **kw: ("", {"rows": [{"slot_minute": 600, "cost_change": 0.0}]})
    my_predbat.rate_import_replicated = {600 - 24 * 60: "saving"}
    try:
        my_predbat.calculate_yesterday()
        return my_predbat.get_state_wrapper(my_predbat.prefix + ".cost_yesterday", attribute="json") or {}
    finally:
        _restore_methods(my_predbat, original_run_pred)
        my_predbat.savings_last_updated = None
        my_predbat.rate_import_replicated = {}


def _test_yesterday_json_has_billing(my_predbat, failed):
    """Yesterday's History view carries the billing, with known events labelled."""
    print("history_days: yesterday json has billing")
    _yesterday_billing(my_predbat, {"import": True, "export": None})
    try:
        plan = _run_yesterday_with_rows(my_predbat)
    finally:
        my_predbat.dashboard_item(my_predbat.prefix + ".billing", state=None, attributes={})
    row = (plan.get("rows") or [{}])[0]
    if "billing_summary" not in plan or row.get("billed") != 0.1 or row.get("billing_event") != "Saving event":
        print("ERROR: yesterday's 10:00 row should be billed 0.1 and labelled Saving event, got {} / {}".format(row, plan.get("billing_summary")))
        failed = True
    return failed


def _test_import_only_when_export_leg_missing(my_predbat, failed):
    """With export billing unreadable, yesterday compares import only, and shows nothing without import cost history."""
    print("history_days: import-only when the export leg failed")
    _yesterday_billing(my_predbat, {"import": True, "export": False})
    try:
        plan = _run_yesterday_with_rows(my_predbat)
        plan_without = _run_yesterday_with_rows(my_predbat, missing=(".cost_today_import",))
    finally:
        my_predbat.dashboard_item(my_predbat.prefix + ".billing", state=None, attributes={})
    if (plan.get("billing_summary") or {}).get("import_only") is not True:
        print("ERROR: an unreadable export leg should make the comparison import-only, got {}".format(plan.get("billing_summary")))
        failed = True
    if "billing_summary" in plan_without:
        print("ERROR: import-only with no cost_today_import history should show no billing, got {}".format(plan_without.get("billing_summary")))
        failed = True
    return failed


def _test_history_days_real_replay(my_predbat, failed):
    """With the real replay and renderer, each older day is a full table of slot rows, with billing attached."""
    print("history_days: real replay of the older days")
    from tests.test_calculate_yesterday import _make_mock_step_data

    now_utc = _setup_base(my_predbat, minutes_now=360)
    my_predbat.step_data_history = _make_mock_step_data(my_predbat.pv_today)
    my_predbat.get_history_wrapper = _history_mock(my_predbat, now_utc)
    my_predbat.max_days_previous = 8
    my_predbat.plan_interval_minutes = 30
    my_predbat.history_days_built_for = None
    my_predbat.history_days_billing_mark = None
    rate_import_before = my_predbat.rate_import
    midnight_before = my_predbat.midnight_utc
    day_start = my_predbat.local_tz.localize(datetime(2024, 10, 2)).astimezone(timezone.utc)
    slot = {"kwh": 1.0, "cost": 0.0, "standing": 1.0, "buckets": {"Home Low Rate": {"kwh": 1.0, "cost": 0.0}}}
    slots = {(day_start + timedelta(minutes=30 * n)).strftime("%Y-%m-%dT%H:%M:%SZ"): dict(slot) for n in range(48)}
    my_predbat.dashboard_item(my_predbat.prefix + ".billing", state=0.0, attributes={"complete_until": "2024-10-03T00:00:00Z", "slots": {"import": slots}, "legs": {"import": True, "export": None}, "supplier": "octopus"})
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        del my_predbat.step_data_history
        del my_predbat.get_history_wrapper
        my_predbat.dashboard_item(my_predbat.prefix + ".billing", state=None, attributes={})
    if len(days) != 6 or any(len(day.get("rows") or []) != 48 for day in days.values()):
        print("ERROR: expected 6 older days of 48 rows, got {}".format({name: len(day.get("rows") or []) for name, day in days.items()}))
        failed = True
    elif "billing_summary" not in days["2024-10-02"] or "billing_summary" in days["2024-10-01"]:
        print("ERROR: only 2 Oct has billing and only it should carry a billing summary")
        failed = True
    elif not str(days["2024-10-02"]["rows"][0].get("time", "")).startswith("2024-10-02T00:00:00+01"):
        print("ERROR: an older day's first row should be local midnight 2024-10-02T00:00:00+0100, got {}".format(days["2024-10-02"]["rows"][0].get("time")))
        failed = True
    elif days["2024-10-02"]["rows"][0].get("import_rate") != 25.0:
        print("ERROR: older-day rows should show the recorded 25p rate, got {}".format(days["2024-10-02"]["rows"][0].get("import_rate")))
        failed = True
    if my_predbat.rate_import is not rate_import_before or my_predbat.midnight_utc != midnight_before:
        print("ERROR: the plan's rates and midnight must be put back after building the older days")
        failed = True
    return failed


def _test_history_row_costs_strip_next_day_standing(my_predbat, failed):
    """The row ending at midnight reads 4 minutes into the next day, so it carries the next day's standing charge; it is taken off there, not at 00:00."""
    print("history_days: standing charge stripped from the row that crosses midnight")
    my_predbat.plan_interval_minutes = 30
    array = {minute: 55.52 + minute * 0.1 for minute in range(0, 24 * 60)}
    array[24 * 60] = 55.52 + 1440 * 0.1 + 55.52 + 0.4
    costs = my_predbat.history_row_costs(array, 24 * 60, 55.52)
    if abs(costs[0] - 3.0) > 0.001 or abs(costs[1410] - 3.4) > 0.001:
        print("ERROR: 00:00 should cost 3p and 23:30 3.4p once the next day's standing is taken off, got {} and {}".format(costs[0], costs[1410]))
        failed = True
    yesterday = {minute: (55.52 + minute * 0.1 if minute < 1440 else 55.52 + 144.0 + 55.52 + (minute - 1440) * 0.1 + 0.4) for minute in range(0, 1440 + 360 + 1)}
    costs = my_predbat.history_row_costs(yesterday, 1440 + 360, 55.52)
    if abs(costs[1410] - 3.4) > 0.001 or abs(costs[1440] - 3.0) > 0.001:
        print("ERROR: across midnight in the yesterday + today view the 23:30 row should be 3.4p and 00:00 3p, got {} and {}".format(costs[1410], costs[1440]))
        failed = True
    import_only = my_predbat.history_row_costs(array, 24 * 60, 0.0)
    if abs(import_only[1410] - (3.4 + 55.52)) > 0.001:
        print("ERROR: with no standing charge to strip, the row cost is left as it is")
        failed = True
    return failed


def run_history_days_tests(my_predbat):
    """Run the History replay and 7-day History tests."""
    failed = False
    print("**** Running history_days tests ****")
    failed = _test_replay_called_by_calculate_yesterday(my_predbat, failed)
    failed = _test_history_days_built_once_per_day(my_predbat, failed)
    failed = _test_history_days_rebuild_on_new_billing(my_predbat, failed)
    failed = _test_history_days_limited_by_history(my_predbat, failed)
    failed = _test_history_days_rates_from_history(my_predbat, failed)
    failed = _test_yesterday_json_has_billing(my_predbat, failed)
    failed = _test_import_only_when_export_leg_missing(my_predbat, failed)
    failed = _test_history_days_real_replay(my_predbat, failed)
    failed = _test_history_row_costs_strip_next_day_standing(my_predbat, failed)
    return failed
