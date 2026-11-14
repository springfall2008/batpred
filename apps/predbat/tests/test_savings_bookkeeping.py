# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Daily savings can be reused without scheduled device/state housekeeping."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

TOTALS = ("savings_total_predbat", "savings_total_pvbat", "savings_total_actual", "savings_total_soc", "cost_total_car", "carbon_total")


def _engine(*, minutes=61, readonly=False, calculate=True, optional=True, last="2026-11-08", states=None):
    """A real PredBat instance with only state persistence replaced by a recording store."""
    from predbat import PredBat

    engine = object.__new__(PredBat)
    store = deepcopy(states) if states is not None else {"predbat." + name: {"state": 1000.0, "attributes": {"last_updated": last, "start_date": "2026-01-01"}} for name in TOTALS}
    engine.prefix = "predbat"
    engine.calculate_savings = calculate
    engine.set_read_only = readonly
    engine.minutes_now = minutes
    engine.now_utc_real = datetime(2026, 11, 9, 1, 1, tzinfo=timezone.utc)
    engine.num_cars = int(optional)
    engine.carbon_enable = optional
    engine.currency_symbols = ("£", "p")
    engine.savings_today_predbat = 329.99
    engine.savings_today_pvbat = 413.4
    engine.savings_today_actual = 942.63
    engine.savings_today_predbat_soc = 0.54
    engine.cost_yesterday_car = 115.5
    engine.carbon_yesterday = 1350.25
    writes = {}

    def previous(entity, attribute=None):
        """Read the store, including values emitted earlier in this cycle."""
        row = store.get(entity, {})
        return row.get("attributes", {}).get(attribute) if attribute else row.get("state")

    def emit(entity, state, attributes):
        """Record and synchronously expose an emitted entity to later reads."""
        row = {"state": state, "attributes": dict(attributes)}
        store[entity] = row
        writes[entity] = row

    engine.load_previous_value_from_ha = previous
    engine.dashboard_item = emit
    return engine, store, writes


def run_savings_bookkeeping_tests(my_predbat):
    """Verify concrete daily totals, boundary policy, repeated calls and persisted retries."""
    failures = 0

    def check(condition, description):
        """Report an independently expected accounting outcome to the standard runner."""
        nonlocal failures
        if not condition:
            print("ERROR: savings bookkeeping: " + description)
            failures += 1

    engine, store, writes = _engine()
    engine.update_savings_totals(scheduled=True)
    expected = (1329.99, 1413.4, 1942.63, 0.54, 1115.5, 2350.25)
    check(tuple(writes["predbat." + name]["state"] for name in TOTALS) == expected, "daily values, including car and carbon")
    check(writes["predbat.savings_total_predbat"]["attributes"]["pounds"] == 13.3, "currency conversion")
    check(writes["predbat.carbon_total"]["attributes"]["kg"] == 2.35, "carbon conversion")
    committed = deepcopy(store)
    engine.update_savings_totals(scheduled=True)
    check(store == committed, "same-cycle repeat cannot advance twice")
    retry, retried, _ = _engine()
    retry.update_savings_totals(scheduled=True)
    check(retried == committed, "retry with uncommitted original rows yields the same values")
    restored, persisted, _ = _engine(states=committed)
    restored.update_savings_totals(scheduled=True)
    check(persisted == committed, "retry after commit does not add again")
    engine.now_utc_real += timedelta(days=1)
    engine.update_savings_totals(scheduled=True)
    check(store["predbat.savings_total_predbat"]["state"] == 1659.98, "next date can advance once again")
    check(store["predbat.savings_total_predbat"]["attributes"]["last_updated"] == "2026-11-10", "rollover marker")

    for changes in ({"minutes": 0}, {"minutes": 60}, {"readonly": True}, {"last": "2026-11-09"}):
        held, _, emitted = _engine(**changes)
        held.update_savings_totals(scheduled=True)
        check(emitted["predbat.savings_total_predbat"]["state"] == 1000.0, "guard " + str(changes))
    held, _, emitted = _engine()
    held.update_savings_totals(scheduled=False)
    check(emitted["predbat.savings_total_predbat"]["state"] == 1000.0, "unscheduled path publishes but does not advance")
    held, _, emitted = _engine(calculate=False)
    held.update_savings_totals(scheduled=True)
    check(not emitted, "disabled savings publishes nothing")
    held, _, emitted = _engine(optional=False)
    held.update_savings_totals(scheduled=True)
    check("predbat.cost_total_car" not in emitted and "predbat.carbon_total" not in emitted, "optional ledgers remain absent")
    held, _, emitted = _engine(last=None)
    held.update_savings_totals(scheduled=True)
    check(emitted["predbat.savings_total_predbat"]["state"] == 1000.0, "new install does not add a prior day")
    check(emitted["predbat.savings_total_predbat"]["attributes"]["last_updated"] == "2026-11-09", "new install sets marker")
    held, _, emitted = _engine(last=None, states={})
    held.update_savings_totals(scheduled=True)
    check(emitted["predbat.savings_total_predbat"]["state"] == 0.0, "missing previous values initialise at zero")
    held, _, emitted = _engine()
    held.now_utc_real = datetime(2026, 11, 10, 1, 1, tzinfo=timezone(timedelta(hours=13)))
    held.update_savings_totals(scheduled=True)
    check(emitted["predbat.savings_total_predbat"]["attributes"]["last_updated"] == "2026-11-10", "date follows engine clock, not wall UTC")
    failures += _check_bookkeeping_call_order(my_predbat)
    print("Savings bookkeeping tests: {} failure(s)".format(failures))
    return failures


def _check_bookkeeping_call_order(engine):
    """The complete pod routine calls savings once in its original position and policy."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from tests.test_plan_execute_split import _quiet_bookkeeping

    saved = dict(vars(engine))
    saved["args"] = deepcopy(engine.args)
    for name in ("control_ledger", "dashboard_values", "dashboard_index", "dashboard_index_app"):
        if name in saved:
            saved[name] = deepcopy(saved[name])
    failures = 0
    try:
        _quiet_bookkeeping(engine)
        for scheduled in (False, True):
            calls = []
            with ExitStack() as stack:
                stack.enter_context(patch.object(engine, "publish_control_failures", side_effect=lambda *args: calls.append("control")))
                stack.enter_context(patch.object(engine, "update_savings_totals", side_effect=lambda value: calls.append(("savings", value))))
                stack.enter_context(patch.object(engine, "update_car_manual_soc", side_effect=lambda: calls.append("car")))
                for name in ("save_current_config", "_capture_debug_history", "record_final_run_status"):
                    stack.enter_context(patch.object(engine, name))
                engine._post_run_bookkeeping("Demand", "", scheduled, True)
            expected = ["control", ("savings", scheduled)] + (["car"] if scheduled else [])
            if calls != expected:
                print("ERROR: savings bookkeeping call order: {} != {}".format(calls, expected))
                failures += 1
    finally:
        vars(engine).clear()
        vars(engine).update(saved)
    return failures
