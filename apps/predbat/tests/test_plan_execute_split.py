# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Characterisation and unit tests for the plan_once()/execute_once() split of update_pred()."""

from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

_STATE_ATTRS = [
    "plugin_system",
    "iboost_enable",
    "calculate_savings",
    "debug_enable",
    "num_cars",
    "holiday_days_left",
    "comparison",
    "components",
    "rate_min",
    "rate_max",
    "calculate_plan_every",
    "plan_valid",
    "plan_last_updated",
    "had_errors",
    "count_inverter_writes",
]


def _save_state(my_predbat):
    """Snapshot the shared fixture attributes this module mutates, so they can be restored."""
    saved = {name: getattr(my_predbat, name, None) for name in _STATE_ATTRS}
    saved["_template_arg"] = my_predbat.args.get("template", None)
    return saved


def _restore_state(my_predbat, saved):
    """Restore fixture attributes captured by _save_state (the fixture is shared across the whole test run)."""
    for name in _STATE_ATTRS:
        setattr(my_predbat, name, saved[name])
    if saved["_template_arg"] is None:
        my_predbat.args.pop("template", None)
    else:
        my_predbat.args["template"] = saved["_template_arg"]


def _quiet_bookkeeping(my_predbat):
    """Steer update_pred away from optional bookkeeping branches so the mock surface stays small."""
    my_predbat.plugin_system = None
    my_predbat.iboost_enable = False
    my_predbat.calculate_savings = False
    my_predbat.debug_enable = False
    my_predbat.num_cars = 0
    my_predbat.holiday_days_left = 0
    my_predbat.comparison = None
    my_predbat.components = None
    my_predbat.rate_min = 5.0
    my_predbat.rate_max = 30.0
    my_predbat.calculate_plan_every = 10
    my_predbat.had_errors = False
    my_predbat.count_inverter_writes = {}
    my_predbat.args.pop("template", None)


def _patch_pipeline(my_predbat, stack, inverter_ok=True, plan_valid_after=True):
    """Patch the fetch/plan/execute pipeline on my_predbat, returning a dict of the created mocks."""
    mocks = {}
    for name in ("update_time", "save_current_config", "download_predbat_releases", "fetch_config_options", "publish_rate_and_threshold", "save_plan"):
        mocks[name] = stack.enter_context(patch.object(my_predbat, name))
    mocks["fetch_sensor_data"] = stack.enter_context(patch.object(my_predbat, "fetch_sensor_data", return_value=False))
    mocks["dynamic_load"] = stack.enter_context(patch.object(my_predbat, "dynamic_load", return_value=False))
    mocks["fetch_inverter_data"] = stack.enter_context(patch.object(my_predbat, "fetch_inverter_data", return_value=inverter_ok))

    def fake_calculate_plan(recompute=True, debug_mode=False, publish=True):
        """Stand-in for calculate_plan that marks the plan valid when a recompute is requested."""
        if recompute and plan_valid_after:
            my_predbat.plan_valid = True
            my_predbat.plan_last_updated = my_predbat.now_utc
        return recompute

    mocks["calculate_plan"] = stack.enter_context(patch.object(my_predbat, "calculate_plan", side_effect=fake_calculate_plan))
    mocks["execute_plan"] = stack.enter_context(patch.object(my_predbat, "execute_plan", return_value=("Demand", "")))
    return mocks


def _check(failed, condition, message):
    """Print OK/ERROR for a single assertion and accumulate the failure count."""
    if condition:
        print("OK: {}".format(message))
        return failed
    print("ERROR: {}".format(message))
    return failed + 1


def _scenario_fused_fresh_plan(my_predbat):
    """Characterise: scheduled run with a fresh valid plan reuses it - no recompute, no save, single execute."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.update_pred(scheduled=True)
    failed = _check(failed, mocks["calculate_plan"].call_count == 1, "fresh plan: calculate_plan called once")
    failed = _check(failed, mocks["calculate_plan"].call_args.kwargs.get("recompute") is False, "fresh plan: calculate_plan called with recompute=False")
    failed = _check(failed, mocks["save_plan"].call_count == 0, "fresh plan: save_plan not called")
    failed = _check(failed, mocks["execute_plan"].call_count == 1, "fresh plan: execute_plan called once")
    failed = _check(failed, mocks["fetch_inverter_data"].call_count == 1, "fresh plan: inverter data fetched once (no refetch)")
    return failed


def _scenario_fused_recompute(my_predbat):
    """Characterise: invalid plan forces a recompute - save_plan called, inverter refetched before execute."""
    failed = 0
    my_predbat.plan_valid = False
    my_predbat.plan_last_updated = None
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.update_pred(scheduled=True)
    failed = _check(failed, mocks["calculate_plan"].call_count == 1, "recompute: calculate_plan called once")
    failed = _check(failed, mocks["calculate_plan"].call_args.kwargs.get("recompute") is True, "recompute: calculate_plan called with recompute=True")
    failed = _check(failed, mocks["save_plan"].call_count == 1, "recompute: save_plan called once")
    failed = _check(failed, mocks["execute_plan"].call_count == 1, "recompute: execute_plan called once")
    failed = _check(failed, mocks["fetch_inverter_data"].call_count == 2, "recompute: inverter data fetched twice (pre-plan and refetch)")
    return failed


def _scenario_fused_aged_plan(my_predbat):
    """Characterise: an aged valid plan executes first, then recomputes and executes again (and does NOT save - existing quirk)."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc - timedelta(minutes=9)
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.update_pred(scheduled=True)
    failed = _check(failed, mocks["calculate_plan"].call_count == 2, "aged plan: calculate_plan called twice")
    first_kwargs = mocks["calculate_plan"].call_args_list[0].kwargs
    second_kwargs = mocks["calculate_plan"].call_args_list[1].kwargs
    failed = _check(failed, first_kwargs.get("recompute") is False, "aged plan: first calculate_plan with recompute=False")
    failed = _check(failed, second_kwargs.get("recompute") is True, "aged plan: second calculate_plan with recompute=True")
    failed = _check(failed, mocks["execute_plan"].call_count == 2, "aged plan: execute_plan called twice")
    failed = _check(failed, mocks["fetch_inverter_data"].call_count == 2, "aged plan: inverter data fetched twice")
    # Existing upstream quirk: the aged-plan recompute does not persist via save_plan. Preserve, do not fix.
    failed = _check(failed, mocks["save_plan"].call_count == 0, "aged plan: save_plan not called (existing quirk preserved)")
    return failed


def _scenario_fused_inverter_failure(my_predbat):
    """Characterise: inverter fetch failure aborts the run before planning or executing."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    my_predbat.had_errors = False
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, inverter_ok=False)
        my_predbat.update_pred(scheduled=True)
    failed = _check(failed, mocks["calculate_plan"].call_count == 0, "inverter failure: calculate_plan not called")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "inverter failure: execute_plan not called")
    failed = _check(failed, my_predbat.had_errors, "inverter failure: had_errors set")
    return failed


def _scenario_fused_zero_rates(my_predbat):
    """Characterise: all-zero import rates abort the run before planning or executing."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    my_predbat.had_errors = False
    my_predbat.rate_min = 0
    my_predbat.rate_max = 0
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.update_pred(scheduled=True)
    my_predbat.rate_min = 5.0
    my_predbat.rate_max = 30.0
    failed = _check(failed, mocks["calculate_plan"].call_count == 0, "zero rates: calculate_plan not called")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "zero rates: execute_plan not called")
    failed = _check(failed, my_predbat.had_errors, "zero rates: had_errors set")
    return failed


def _scenario_fused_template(my_predbat):
    """Characterise: template configuration aborts the run before any fetching."""
    failed = 0
    my_predbat.had_errors = False
    my_predbat.args["template"] = True
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.update_pred(scheduled=True)
    my_predbat.args.pop("template", None)
    failed = _check(failed, mocks["fetch_config_options"].call_count == 0, "template: fetch_config_options not called")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "template: execute_plan not called")
    failed = _check(failed, my_predbat.had_errors, "template: had_errors set")
    return failed


def test_plan_execute_split(my_predbat):
    """Entry point registered in unit_test.py - characterises update_pred and tests plan_once/execute_once."""
    print("**** Running plan/execute split tests ****")
    failed = 0
    saved = _save_state(my_predbat)
    try:
        _quiet_bookkeeping(my_predbat)
        failed += _scenario_fused_fresh_plan(my_predbat)
        failed += _scenario_fused_recompute(my_predbat)
        failed += _scenario_fused_aged_plan(my_predbat)
        failed += _scenario_fused_inverter_failure(my_predbat)
        failed += _scenario_fused_zero_rates(my_predbat)
        failed += _scenario_fused_template(my_predbat)
    finally:
        _restore_state(my_predbat, saved)
    return failed
