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
    my_predbat.history_days_retry_at = None
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
            "sensor." + my_predbat.prefix + "_billing",
            state=1.0,
            attributes={"complete_until": "2024-10-02T00:00:00Z", "slots": {"import": {"2024-10-01T10:00:00Z": {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}}}, "legs": {"import": True, "export": None}, "supplier": "octopus"},
        )
        my_predbat.calculate_history_days()
        first = len(calls)
        my_predbat.dashboard_item(
            "sensor." + my_predbat.prefix + "_billing",
            state=1.0,
            attributes={
                "complete_until": "2024-10-03T00:00:00Z",
                "slots": {"import": {"2024-10-01T10:00:00Z": {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}, "2024-10-01T10:30:00Z": {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}}},
                "legs": {"import": True, "export": None},
                "supplier": "octopus",
            },
        )
        my_predbat.calculate_history_days()
        second = len(calls) - first
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    if first != 6 or second != 6:
        print("ERROR: new billing should rebuild the older days, got {} then {}".format(first, second))
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
    """Publish a sensor.predbat_billing covering yesterday 10:00-10:30 UTC (the _setup_base day)."""
    slot = {"kwh": 1.0, "cost": 10.0, "standing": 1.0, "buckets": {"Home Low Rate": {"kwh": 1.0, "cost": 10.0}}}
    my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=10.0, attributes={"complete_until": "2024-10-03T10:30:00Z", "slots": {"import": {"2024-10-03T10:00:00Z": slot}}, "legs": legs, "supplier": "octopus"})


def _run_yesterday_with_rows(my_predbat, missing=(), events=None):
    """Run calculate_yesterday with a renderer that returns one row at 10:00 and return the published json; events adds recorded event entity histories."""
    now_utc = _setup_base(my_predbat, minutes_now=360)
    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)
    my_predbat.get_history_wrapper = _with_event_histories(_history_mock(my_predbat, now_utc, missing=missing), events or {})
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
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    row = (plan.get("rows") or [{}])[0]
    if "billing_summary" not in plan or row.get("billed") != 0.1 or row.get("billing_event") != "Saving event":
        print("ERROR: yesterday's 10:00 row should be billed 0.1 and labelled Saving event, got {} / {}".format(row, plan.get("billing_summary")))
        failed = True
    return failed


def _test_import_only_when_export_leg_missing(my_predbat, failed):
    """With an export meter whose billing can't be read, the read counts as failed: no billing is shown rather than an import-only comparison."""
    print("history_days: import-only when the export leg failed")
    _yesterday_billing(my_predbat, {"import": True, "export": False})
    try:
        plan = _run_yesterday_with_rows(my_predbat)
        plan_without = _run_yesterday_with_rows(my_predbat, missing=(".cost_today_import",))
    finally:
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    if "billing_summary" in plan:
        print("ERROR: with an export meter whose billing failed to read, rows without export billing should show none, got {}".format(plan.get("billing_summary")))
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
    slot = {"kwh": 0.4, "cost": 10.0, "standing": 1.0, "buckets": {"Home Low Rate": {"kwh": 0.4, "cost": 10.0}}}
    slots = {(day_start + timedelta(minutes=30 * n)).strftime("%Y-%m-%dT%H:%M:%SZ"): dict(slot) for n in range(48)}
    my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=0.0, attributes={"complete_until": "2024-10-03T00:00:00Z", "slots": {"import": slots}, "legs": {"import": True, "export": None}, "supplier": "octopus"})
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        del my_predbat.step_data_history
        del my_predbat.get_history_wrapper
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
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
    costs = my_predbat.history_row_costs(array, 24 * 60, 55.52, [24 * 60])
    if abs(costs[0] - 3.0) > 0.001 or abs(costs[1410] - 3.4) > 0.001:
        print("ERROR: 00:00 should cost 3p and 23:30 3.4p once the next day's standing is taken off, got {} and {}".format(costs[0], costs[1410]))
        failed = True
    yesterday = {minute: (55.52 + minute * 0.1 if minute < 1440 else 55.52 + 144.0 + 55.52 + (minute - 1440) * 0.1 + 0.4) for minute in range(0, 1440 + 360 + 1)}
    costs = my_predbat.history_row_costs(yesterday, 1440 + 360, 55.52, [24 * 60])
    if abs(costs[1410] - 3.4) > 0.001 or abs(costs[1440] - 3.0) > 0.001:
        print("ERROR: across midnight in the yesterday + today view the 23:30 row should be 3.4p and 00:00 3p, got {} and {}".format(costs[1410], costs[1440]))
        failed = True
    import_only = my_predbat.history_row_costs(array, 24 * 60, 0.0, [24 * 60])
    if abs(import_only[1410] - (3.4 + 55.52)) > 0.001:
        print("ERROR: with no standing charge to strip, the row cost is left as it is")
        failed = True
    return failed


def _iog_rate(my_predbat, when):
    """IOG-style import rate at a UTC instant: 6.6p 23:30-05:30 local, 36.28p otherwise."""
    local = when.astimezone(my_predbat.local_tz)
    minutes = local.hour * 60 + local.minute
    return 6.6 if (minutes >= 23 * 60 + 30 or minutes < 5 * 60 + 30) else 36.28


def _realistic_histories(my_predbat, now_utc, days=10):
    """Recorder-like histories every 5 minutes: cost_today accrues 0.4 kWh per half-hour at the IOG rate and resets (to the standing charge) at local midnight."""
    start = (now_utc - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    cost, import_cost, rates, socs = [], [], [], []
    total = 0.0
    import_total = 0.0
    when = start
    local_day = when.astimezone(my_predbat.local_tz).date()
    while when <= now_utc:
        this_day = when.astimezone(my_predbat.local_tz).date()
        if this_day != local_day:
            local_day = this_day
            total = 55.52
            import_total = 0.0
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        cost.append({"state": str(round(total, 4)), "last_updated": stamp, "attributes": {}})
        import_cost.append({"state": str(round(import_total, 4)), "last_updated": stamp, "attributes": {}})
        rates.append({"state": str(_iog_rate(my_predbat, when)), "last_updated": stamp, "attributes": {}})
        socs.append({"state": "5.0", "last_updated": stamp, "attributes": {}})
        for minute in range(5):
            total += _iog_rate(my_predbat, when + timedelta(minutes=minute)) * 0.4 / 30.0
            import_total += _iog_rate(my_predbat, when + timedelta(minutes=minute)) * 0.4 / 30.0
        when += timedelta(minutes=5)
    histories = {".cost_today": [cost], ".cost_today_import": [import_cost], ".rates": [rates], ".rates_export": [[dict(point, state="15.0") for point in rates]], ".soc_kw_h0": [socs]}
    prefix = my_predbat.prefix
    return lambda entity_id, days=30, required=True, tracked=True: histories.get(entity_id[len(prefix) :] if entity_id.startswith(prefix) else entity_id)


def _matching_billing(my_predbat, start_utc, end_utc):
    """Billing that matches the realistic cost_today exactly: 0.4 kWh per half-hour at the IOG rate.

    The standing charge is spread over each London day's real half-hours, as Octopus does (1.11047p a slot on 25 Oct's 50).
    """
    from billing import _half_hours_in_day

    slots = {}
    day_half_hours = {}
    when = start_utc
    while when < end_utc:
        rate = _iog_rate(my_predbat, when)
        key = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        standing = round(55.52 / _half_hours_in_day(key, day_half_hours), 5)
        slots[key] = {"kwh": 0.4, "cost": round(0.4 * rate, 4), "standing": standing, "buckets": {"Rate": {"kwh": 0.4, "cost": round(0.4 * rate, 4)}}}
        when += timedelta(minutes=30)
    return {"complete_until": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "slots": {"import": slots}, "legs": {"import": True, "export": None}, "supplier": "octopus"}


def _set_clock(my_predbat, local_now):
    """Put the base's clock at a local time (pytz-aware), as update_time() does."""
    now_local = my_predbat.local_tz.localize(local_now)
    my_predbat.now_utc = now_local
    my_predbat.midnight_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((now_local - my_predbat.midnight_utc).total_seconds() // 60)
    return now_local.astimezone(timezone.utc)


def _older_clock_change_day(my_predbat, local_now, day, expected_rows):
    """Build the older days around a clock change and return (failed, rows) for day."""
    from tests.test_calculate_yesterday import _make_mock_step_data

    _setup_base(my_predbat, minutes_now=360)
    now_utc = _set_clock(my_predbat, local_now)
    my_predbat.step_data_history = _make_mock_step_data(my_predbat.pv_today)
    my_predbat.get_history_wrapper = _realistic_histories(my_predbat, now_utc)
    my_predbat.max_days_previous = 8
    my_predbat.plan_interval_minutes = 30
    my_predbat.metric_standing_charge = 55.52
    my_predbat.history_days_built_for = None
    my_predbat.history_days_billing_mark = None
    day_start = my_predbat.local_midnight_utc(day).astimezone(timezone.utc)
    day_end = my_predbat.local_midnight_utc(day + timedelta(days=1)).astimezone(timezone.utc)
    my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=0.0, attributes=_matching_billing(my_predbat, day_start, day_end))
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        del my_predbat.step_data_history
        del my_predbat.get_history_wrapper
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    rows = (days.get(day.isoformat()) or {}).get("rows") or []
    if len(rows) != expected_rows:
        print("ERROR: {} should have {} rows, got {}".format(day, expected_rows, len(rows)))
        return True, rows
    bad = [(row.get("time"), row.get("billing_diff")) for row in rows if row.get("billing_diff") is None or abs(row["billing_diff"]) > 0.01]
    if bad:
        print("ERROR: {} billing matches Predbat's cost, so every Diff should be ~0, got {}".format(day, bad[:6]))
        return True, rows
    summary = days[day.isoformat()].get("billing_summary") or {}
    if abs(summary.get("diff", 1)) > 0.02 or summary.get("standing_predbat") != summary.get("standing_billed"):
        print("ERROR: {} summary should match, got {}".format(day, summary))
        return True, rows
    return False, rows


def _test_clock_change_older_days(my_predbat, failed):
    """Older days of 50 and 46 half-hours compare cleanly and are labelled in local time."""
    from datetime import date

    print("history_days: clock-change older days")
    autumn_failed, rows = _older_clock_change_day(my_predbat, datetime(2026, 10, 27, 6, 0), date(2026, 10, 25), 50)
    failed = failed or autumn_failed
    if rows and (not str(rows[0]["time"]).startswith("2026-10-25T00:00:00+01") or not str(rows[-1]["time"]).startswith("2026-10-25T23:30:00+00")):
        print("ERROR: 25 Oct rows should run 00:00 BST to 23:30 GMT, got {} to {}".format(rows[0]["time"], rows[-1]["time"]))
        failed = True
    spring_failed, rows = _older_clock_change_day(my_predbat, datetime(2026, 3, 31, 6, 0), date(2026, 3, 29), 46)
    failed = failed or spring_failed
    if rows and (not str(rows[0]["time"]).startswith("2026-03-29T00:00:00+00") or not str(rows[-1]["time"]).startswith("2026-03-29T23:30:00+01")):
        print("ERROR: 29 Mar rows should run 00:00 GMT to 23:30 BST, got {} to {}".format(rows[0]["time"], rows[-1]["time"]))
        failed = True
    return failed


def _test_clock_change_yesterday_view(my_predbat, failed):
    """The day after the clocks go forward, yesterday + today compares cleanly across the shifted midnight."""
    print("history_days: clock-change yesterday + today view")
    _setup_base(my_predbat, minutes_now=360)
    now_utc = _set_clock(my_predbat, datetime(2026, 3, 30, 6, 0))
    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)
    my_predbat.get_history_wrapper = _realistic_histories(my_predbat, now_utc)
    my_predbat.plan_interval_minutes = 30
    my_predbat.metric_standing_charge = 55.52
    view_minutes = 24 * 60 + my_predbat.minutes_now
    my_predbat.publish_html_plan = lambda *a, **kw: ("", {"rows": [{"slot_minute": minute, "cost_change": 0.0} for minute in range(0, view_minutes, 30)]})
    view_start = (my_predbat.midnight_utc - timedelta(days=1)).astimezone(timezone.utc)
    my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=0.0, attributes=_matching_billing(my_predbat, view_start, now_utc - timedelta(hours=1)))
    try:
        my_predbat.calculate_yesterday()
        plan = my_predbat.get_state_wrapper(my_predbat.prefix + ".cost_yesterday", attribute="json") or {}
    finally:
        _restore_methods(my_predbat, original_run_pred)
        my_predbat.savings_last_updated = None
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    billed = [row for row in plan.get("rows") or [] if row.get("billed") is not None]
    bad = [(row["slot_minute"], row.get("billing_diff")) for row in billed if row.get("billing_diff") is None or abs(row["billing_diff"]) > 0.01]
    if len(billed) < 40 or bad:
        print("ERROR: across the 29 Mar clock change every billed row should match, got {} billed, mismatches {}".format(len(billed), bad[:6]))
        failed = True
    return failed


def _test_import_only_when_export_not_read(my_predbat, failed):
    """With no export billing read at all (no export meter known), the comparison is import-only, never net cost against an import bill."""
    print("history_days: import-only when export isn't read")
    _yesterday_billing(my_predbat, {"import": True, "export": None})
    try:
        plan = _run_yesterday_with_rows(my_predbat)
    finally:
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    if (plan.get("billing_summary") or {}).get("import_only") is not True:
        print("ERROR: export not read should compare import only, got {}".format(plan.get("billing_summary")))
        failed = True
    return failed


def _test_older_day_needs_cost_history(my_predbat, failed):
    """An older day that cost_today history doesn't reach is left out rather than shown with 0p Predbat costs."""
    print("history_days: older days need cost_today history")
    from tests.test_calculate_yesterday import _make_mock_step_data

    _setup_base(my_predbat, minutes_now=360)
    now_utc = _set_clock(my_predbat, datetime(2026, 11, 2, 6, 0))
    my_predbat.step_data_history = _make_mock_step_data(my_predbat.pv_today)
    full = _realistic_histories(my_predbat, now_utc)
    short = _realistic_histories(my_predbat, now_utc, days=4)
    my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True: (short if entity_id.endswith(".cost_today") else full)(entity_id, days, required, tracked)
    my_predbat.max_days_previous = 8
    my_predbat.history_days_built_for = None
    my_predbat.history_days_billing_mark = None
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        del my_predbat.step_data_history
        del my_predbat.get_history_wrapper
    if sorted(days) != ["2026-10-30", "2026-10-31"]:
        print("ERROR: cost_today history from 06:00 on 29 Oct covers only 30 and 31 Oct in full, got {}".format(sorted(days)))
        failed = True
    return failed


def _test_history_days_retry_after_failure(my_predbat, failed):
    """A build that produced nothing (history not there yet) is tried again a few hours later, not left until tomorrow."""
    print("history_days: retried a few hours after a failed build")
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat, missing=(".rates",))
    attempts = []
    real_build = my_predbat.build_history_days

    def _counting_build(*args, **kwargs):
        """Count build attempts."""
        attempts.append(my_predbat.now_utc)
        return real_build(*args, **kwargs)

    my_predbat.build_history_days = _counting_build
    my_predbat.history_days_retry_at = None
    try:
        my_predbat.calculate_history_days()
        my_predbat.now_utc = now_utc + timedelta(hours=1)
        my_predbat.calculate_history_days()
        my_predbat.now_utc = now_utc + timedelta(hours=3, minutes=1)
        my_predbat.calculate_history_days()
    finally:
        del my_predbat.build_history_days
        _cleanup_history_days(my_predbat, original_run_pred)
        my_predbat.now_utc = now_utc
    if len(attempts) != 2:
        print("ERROR: expected a build, none an hour later, and a retry after 3 hours; got {} attempts".format(len(attempts)))
        failed = True
    return failed


def _test_rebuild_on_filled_gap(my_predbat, failed):
    """A late half-hour filling a gap (complete_until unchanged) still rebuilds the older days."""
    print("history_days: rebuilt when a gap is filled")
    now_utc, calls, original_run_pred = _prepare_history_days(my_predbat)
    slot = {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}
    try:
        my_predbat.dashboard_item(
            "sensor." + my_predbat.prefix + "_billing",
            state=1.0,
            attributes={"complete_until": "2024-10-01T10:00:00Z", "slots": {"import": {"2024-10-01T09:30:00Z": slot, "2024-10-01T11:00:00Z": slot}}, "legs": {"import": True, "export": None}, "supplier": "octopus"},
        )
        my_predbat.calculate_history_days()
        first = len(calls)
        my_predbat.dashboard_item(
            "sensor." + my_predbat.prefix + "_billing",
            state=1.0,
            attributes={"complete_until": "2024-10-01T10:00:00Z", "slots": {"import": {"2024-10-01T09:30:00Z": slot, "2024-10-01T10:30:00Z": slot, "2024-10-01T11:00:00Z": slot}}, "legs": {"import": True, "export": None}, "supplier": "octopus"},
        )
        my_predbat.calculate_history_days()
        second = len(calls) - first
    finally:
        _cleanup_history_days(my_predbat, original_run_pred)
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    if first != 6 or second != 6:
        print("ERROR: a filled gap should rebuild the older days, got {} then {}".format(first, second))
        failed = True
    return failed


EVENT_ARGS = {"octopus_saving_session": "binary_sensor.predbat_octopus_a_saving_session", "octopus_free_electricity": "sensor.predbat_octopus_a_free_electricity", "axle_session": "binary_sensor.predbat_axle_event"}


def _event_history(start_utc, spans, attributes=None):
    """A recorded on/off history from start_utc, switching on and off at each (on, off) UTC instant in spans; the on points carry attributes."""
    points = [{"state": "off", "last_updated": start_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")}]
    for on, off in spans:
        points.append({"state": "on", "last_updated": on.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "attributes": dict(attributes or {})})
        points.append({"state": "off", "last_updated": off.strftime("%Y-%m-%dT%H:%M:%S+00:00")})
    return [points]


def _with_event_histories(base_history, events):
    """Wrap a get_history_wrapper so the event entities in events ({entity_id: history}) return their recorded history."""
    return lambda entity_id, days=30, required=True, tracked=True: events[entity_id] if entity_id in events else base_history(entity_id, days, required, tracked)


def _test_older_days_mark_recorded_events(my_predbat, failed):
    """Older days mark the half-hours a saving session, free electricity or Axle event was recorded on, and list the day's events for the picker."""
    print("history_days: older days mark recorded events")
    from tests.test_calculate_yesterday import _make_mock_step_data

    utc = timezone.utc
    _setup_base(my_predbat, minutes_now=360)
    now_utc = _set_clock(my_predbat, datetime(2026, 11, 2, 6, 0))
    my_predbat.step_data_history = _make_mock_step_data(my_predbat.pv_today)
    start = now_utc - timedelta(days=10)
    # The sensors change a few minutes after the event does: 17:32 for a 17:30 start, 18:33 for an 18:30 end
    events = {
        EVENT_ARGS["octopus_saving_session"]: _event_history(start, [(datetime(2026, 10, 28, 10, 2, tzinfo=utc), datetime(2026, 10, 28, 10, 31, tzinfo=utc)), (datetime(2026, 10, 31, 17, 32, tzinfo=utc), datetime(2026, 10, 31, 18, 33, tzinfo=utc))]),
        EVENT_ARGS["octopus_free_electricity"]: _event_history(start, [(datetime(2026, 10, 30, 13, 1, tzinfo=utc), datetime(2026, 10, 30, 14, 2, tzinfo=utc))]),
        EVENT_ARGS["axle_session"]: _event_history(start, [(datetime(2026, 10, 29, 16, 0, tzinfo=utc), datetime(2026, 10, 29, 16, 30, tzinfo=utc))]),
    }
    my_predbat.get_history_wrapper = _with_event_histories(_realistic_histories(my_predbat, now_utc), events)
    saved_args = {name: my_predbat.args.get(name) for name in EVENT_ARGS}
    my_predbat.args.update(EVENT_ARGS)
    my_predbat.max_days_previous = 8
    my_predbat.plan_interval_minutes = 30
    my_predbat.metric_standing_charge = 55.52
    my_predbat.history_days_built_for = None
    my_predbat.history_days_billing_mark = None
    my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=0.0, attributes=_matching_billing(my_predbat, datetime(2026, 10, 29, tzinfo=utc), datetime(2026, 11, 1, tzinfo=utc)))
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        del my_predbat.step_data_history
        del my_predbat.get_history_wrapper
        for name, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(name, None)
            else:
                my_predbat.args[name] = value
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})

    def _marked(day):
        """The local times of the rows marked with an event on day, with their labels."""
        return [(str(row["time"])[11:16], row.get("billing_event")) for row in (days.get(day) or {}).get("rows") or [] if row.get("billing_event")]

    expected = {
        "2026-10-31": ([("17:30", "Saving event"), ("18:00", "Saving event")], ["Saving event"]),
        "2026-10-30": ([("13:00", "Free electricity"), ("13:30", "Free electricity")], ["Free electricity"]),
        "2026-10-29": ([("16:00", "Axle event")], ["Axle event"]),
        # Not billed, so no row carries the label, but the picker still flags the day
        "2026-10-28": ([], ["Saving event"]),
    }
    for day, (rows, labels) in expected.items():
        if _marked(day) != rows or (days.get(day) or {}).get("history_events") != labels:
            print("ERROR: {} should mark {} and list {}, got {} and {}".format(day, rows, labels, _marked(day), (days.get(day) or {}).get("history_events")))
            failed = True
    if (days.get("2026-10-27") or {}).get("history_events"):
        print("ERROR: 27 Oct had no event, got {}".format(days["2026-10-27"].get("history_events")))
        failed = True
    return failed


def _test_yesterday_marks_recorded_events(my_predbat, failed):
    """Yesterday + today marks a recorded event too, and its own label wins over the rates' generic saving tag."""
    print("history_days: yesterday marks recorded events")
    _yesterday_billing(my_predbat, {"import": True, "export": None})
    axle = _event_history(datetime(2024, 10, 1, tzinfo=timezone.utc), [(datetime(2024, 10, 3, 10, 4, tzinfo=timezone.utc), datetime(2024, 10, 3, 10, 34, tzinfo=timezone.utc))])
    saved = my_predbat.args.get("axle_session")
    my_predbat.args["axle_session"] = EVENT_ARGS["axle_session"]
    try:
        plan = _run_yesterday_with_rows(my_predbat, events={EVENT_ARGS["axle_session"]: axle})
    finally:
        if saved is None:
            my_predbat.args.pop("axle_session", None)
        else:
            my_predbat.args["axle_session"] = saved
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    row = (plan.get("rows") or [{}])[0]
    if row.get("billing_event") != "Axle event" or plan.get("history_events") != ["Axle event"]:
        print("ERROR: yesterday's 10:00 row should be marked with the recorded Axle event, got {} / {}".format(row.get("billing_event"), plan.get("history_events")))
        failed = True
    return failed


def _test_rate_override_sensors(my_predbat, failed):
    """The import and export rate override sensors are on while the current slot's rate is a manual or apps.yaml override, naming which."""
    print("history_days: rate override sensors")
    saved = (my_predbat.rate_import_replicated, my_predbat.rate_export_replicated)
    import_id = "binary_sensor." + my_predbat.prefix + "_rate_override_import"
    export_id = "binary_sensor." + my_predbat.prefix + "_rate_override_export"
    now = my_predbat.minutes_now
    cases = [
        ({now: "manual"}, {now: "increment"}, ("on", "manual"), ("on", "increment")),
        ({now: "user"}, {}, ("on", "user"), ("off", None)),
        # A saving session is flagged by its own entity, and another slot's override isn't this one's
        ({now: "saving", now + 30: "manual"}, {now - 30: "user"}, ("off", None), ("off", None)),
    ]
    try:
        for import_tags, export_tags, want_import, want_export in cases:
            my_predbat.rate_import_replicated = import_tags
            my_predbat.rate_export_replicated = export_tags
            my_predbat.publish_rate_overrides()
            got_import = (my_predbat.get_state_wrapper(import_id), my_predbat.get_state_wrapper(import_id, attribute="tag"))
            got_export = (my_predbat.get_state_wrapper(export_id), my_predbat.get_state_wrapper(export_id, attribute="tag"))
            if got_import != want_import or got_export != want_export:
                print("ERROR: tags {} / {} should give {} / {}, got {} / {}".format(import_tags, export_tags, want_import, want_export, got_import, got_export))
                failed = True
    finally:
        my_predbat.rate_import_replicated, my_predbat.rate_export_replicated = saved
    return failed


def _test_older_days_mark_rate_overrides(my_predbat, failed):
    """Older days mark the half-hours the rate override sensors recorded, with the override's own label."""
    print("history_days: older days mark recorded rate overrides")
    from tests.test_calculate_yesterday import _make_mock_step_data

    utc = timezone.utc
    _setup_base(my_predbat, minutes_now=360)
    now_utc = _set_clock(my_predbat, datetime(2026, 11, 2, 6, 0))
    my_predbat.step_data_history = _make_mock_step_data(my_predbat.pv_today)
    start = now_utc - timedelta(days=10)
    events = {
        "binary_sensor." + my_predbat.prefix + "_rate_override_import": _event_history(start, [(datetime(2026, 10, 31, 9, 4, tzinfo=utc), datetime(2026, 10, 31, 9, 34, tzinfo=utc))], {"tag": "manual"}),
        "binary_sensor." + my_predbat.prefix + "_rate_override_export": _event_history(start, [(datetime(2026, 10, 31, 15, 0, tzinfo=utc), datetime(2026, 10, 31, 16, 3, tzinfo=utc))], {"tag": "increment"}),
    }
    my_predbat.get_history_wrapper = _with_event_histories(_realistic_histories(my_predbat, now_utc), events)
    my_predbat.max_days_previous = 8
    my_predbat.plan_interval_minutes = 30
    my_predbat.metric_standing_charge = 55.52
    my_predbat.history_days_built_for = None
    my_predbat.history_days_billing_mark = None
    my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=0.0, attributes=_matching_billing(my_predbat, datetime(2026, 10, 31, tzinfo=utc), datetime(2026, 11, 1, tzinfo=utc)))
    try:
        my_predbat.calculate_history_days()
        days = my_predbat.get_state_wrapper(my_predbat.prefix + ".plan_history", attribute="days") or {}
    finally:
        del my_predbat.step_data_history
        del my_predbat.get_history_wrapper
        my_predbat.dashboard_item("sensor." + my_predbat.prefix + "_billing", state=None, attributes={})
    day = days.get("2026-10-31") or {}
    marked = [(str(row["time"])[11:16], row.get("billing_event")) for row in day.get("rows") or [] if row.get("billing_event")]
    want = [("09:00", "Manual rate"), ("15:00", "Rate adjustment"), ("15:30", "Rate adjustment")]
    if marked != want or day.get("history_events") != ["Manual rate", "Rate adjustment"]:
        print("ERROR: 31 Oct should mark {} and list Manual rate, Rate adjustment, got {} and {}".format(want, marked, day.get("history_events")))
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
    failed = _test_clock_change_older_days(my_predbat, failed)
    failed = _test_clock_change_yesterday_view(my_predbat, failed)
    failed = _test_import_only_when_export_not_read(my_predbat, failed)
    failed = _test_older_day_needs_cost_history(my_predbat, failed)
    failed = _test_history_days_retry_after_failure(my_predbat, failed)
    failed = _test_rebuild_on_filled_gap(my_predbat, failed)
    failed = _test_older_days_mark_recorded_events(my_predbat, failed)
    failed = _test_yesterday_marks_recorded_events(my_predbat, failed)
    failed = _test_rate_override_sensors(my_predbat, failed)
    failed = _test_older_days_mark_rate_overrides(my_predbat, failed)
    return failed
