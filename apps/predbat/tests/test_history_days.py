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

from output import Output
from tests.test_calculate_yesterday import _setup_base, _apply_mocks, _restore_methods


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


def run_history_days_tests(my_predbat):
    """Run the History replay and 7-day History tests."""
    failed = False
    print("**** Running history_days tests ****")
    failed = _test_replay_called_by_calculate_yesterday(my_predbat, failed)
    return failed
