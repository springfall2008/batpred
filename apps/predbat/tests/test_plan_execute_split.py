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
    "plan_version",
    "plan_last_updated_minutes",
    "charge_window_best",
    "charge_limit_best",
    "export_window_best",
    "export_limits_best",
]


def _save_state(my_predbat):
    """Snapshot the shared fixture attributes this module mutates, so they can be restored."""
    saved = {name: getattr(my_predbat, name, None) for name in _STATE_ATTRS}
    saved["_template_arg"] = my_predbat.args.get("template", None)
    saved["_plan_random_delay_arg"] = my_predbat.args.get("plan_random_delay", None)
    return saved


def _restore_state(my_predbat, saved):
    """Restore fixture attributes captured by _save_state (the fixture is shared across the whole test run)."""
    for name in _STATE_ATTRS:
        setattr(my_predbat, name, saved[name])
    if saved["_template_arg"] is None:
        my_predbat.args.pop("template", None)
    else:
        my_predbat.args["template"] = saved["_template_arg"]
    if saved["_plan_random_delay_arg"] is None:
        my_predbat.args.pop("plan_random_delay", None)
    else:
        my_predbat.args["plan_random_delay"] = saved["_plan_random_delay_arg"]


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
    my_predbat.args["plan_random_delay"] = 0  # avoids the real random sleep in update_pred's aged-plan path


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


def _scenario_plan_once_recompute(my_predbat):
    """plan_once with an invalid plan recomputes, persists, publishes rates and returns the artifact - without executing."""
    failed = 0
    my_predbat.plan_valid = False
    my_predbat.plan_last_updated = None
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=True)
    failed = _check(failed, isinstance(artifact, dict), "plan_once recompute: returns a dict artifact")
    failed = _check(failed, artifact and artifact.get("recomputed") is True, "plan_once recompute: artifact recomputed True")
    failed = _check(failed, artifact and artifact.get("plan_valid") is True, "plan_once recompute: artifact plan_valid True")
    failed = _check(failed, artifact and artifact.get("plan_last_updated") == my_predbat.plan_last_updated, "plan_once recompute: artifact timestamp matches instance state")
    failed = _check(failed, mocks["save_plan"].call_count == 1, "plan_once recompute: save_plan called once")
    failed = _check(failed, mocks["publish_rate_and_threshold"].call_count == 1, "plan_once recompute: rates published")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "plan_once recompute: execute_plan NOT called")
    failed = _check(failed, mocks["fetch_inverter_data"].call_count == 1, "plan_once recompute: inverter fetched once only")
    return failed


def _scenario_plan_once_reuse(my_predbat):
    """plan_once with a fresh valid plan reuses it - recomputed False and nothing persisted."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=True)
    failed = _check(failed, artifact and artifact.get("recomputed") is False, "plan_once reuse: artifact recomputed False")
    failed = _check(failed, mocks["save_plan"].call_count == 0, "plan_once reuse: save_plan not called")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "plan_once reuse: execute_plan NOT called")
    return failed


def _scenario_plan_once_unscheduled_forces(my_predbat):
    """plan_once with scheduled=False forces a recompute even when the plan is fresh and valid."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=False)
    failed = _check(failed, artifact and artifact.get("recomputed") is True, "plan_once unscheduled: recompute forced")
    failed = _check(failed, mocks["calculate_plan"].call_args.kwargs.get("recompute") is True, "plan_once unscheduled: calculate_plan recompute=True")
    return failed


def _scenario_plan_once_failures(my_predbat):
    """plan_once returns None on inverter-fetch failure, zero rates and template configuration."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc

    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, inverter_ok=False)
        artifact = my_predbat.plan_once(scheduled=True)
    failed = _check(failed, artifact is None, "plan_once inverter failure: returns None")
    failed = _check(failed, mocks["calculate_plan"].call_count == 0, "plan_once inverter failure: calculate_plan not called")

    my_predbat.rate_min = 0
    my_predbat.rate_max = 0
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=True)
    my_predbat.rate_min = 5.0
    my_predbat.rate_max = 30.0
    failed = _check(failed, artifact is None, "plan_once zero rates: returns None")

    my_predbat.args["template"] = True
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=True)
    my_predbat.args.pop("template", None)
    failed = _check(failed, artifact is None, "plan_once template: returns None")
    return failed


def _scenario_execute_once(my_predbat):
    """execute_once refetches inverter data when asked, skips when not, and returns None on fetch failure."""
    failed = 0

    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        result = my_predbat.execute_once(refetch_inverter=True)
    failed = _check(failed, result == ("Demand", ""), "execute_once refetch: returns execute_plan result")
    failed = _check(failed, mocks["fetch_inverter_data"].call_count == 1, "execute_once refetch: inverter fetched once")
    failed = _check(failed, mocks["execute_plan"].call_count == 1, "execute_once refetch: execute_plan called once")

    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        result = my_predbat.execute_once(refetch_inverter=False)
    failed = _check(failed, result == ("Demand", ""), "execute_once no refetch: returns execute_plan result")
    failed = _check(failed, mocks["fetch_inverter_data"].call_count == 0, "execute_once no refetch: inverter not fetched")

    my_predbat.had_errors = False
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, inverter_ok=False)
        result = my_predbat.execute_once(refetch_inverter=True)
    failed = _check(failed, result is None, "execute_once fetch failure: returns None")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "execute_once fetch failure: execute_plan not called")
    failed = _check(failed, my_predbat.had_errors, "execute_once fetch failure: had_errors set")
    return failed


class _FakeStorage:
    """Minimal async stand-in for the storage component, capturing save_plan payloads in memory."""

    def __init__(self):
        """Create the empty in-memory store."""
        self.saved = {}

    async def save(self, namespace, key, data, format="json", expiry=None):
        """Record the payload under (namespace, key)."""
        self.saved[(namespace, key)] = data

    async def load(self, namespace, key):
        """Return the previously saved payload, or None."""
        return self.saved.get((namespace, key))


class _FakeComponents:
    """Stub component registry exposing only a storage component."""

    def __init__(self, storage):
        """Wrap the given storage stub."""
        self._storage = storage

    def get_component(self, name):
        """Return the storage stub for 'storage', None otherwise."""
        return self._storage if name == "storage" else None


def _scenario_plan_version_bump(my_predbat):
    """plan_once bumps plan_version only when a recompute produced a valid plan."""
    failed = 0
    my_predbat.plan_version = 0

    my_predbat.plan_valid = False
    my_predbat.plan_last_updated = None
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=True)
    failed = _check(failed, my_predbat.plan_version == 1, "plan version: bumped to 1 on recompute")
    failed = _check(failed, artifact and artifact.get("plan_version") == 1, "plan version: artifact carries version 1")

    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=True)
    failed = _check(failed, my_predbat.plan_version == 1, "plan version: unchanged on plan reuse")

    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack)
        artifact = my_predbat.plan_once(scheduled=False)
    failed = _check(failed, my_predbat.plan_version == 2, "plan version: bumped to 2 on forced recompute")
    return failed


def _scenario_plan_version_persistence(my_predbat):
    """save_plan persists plan_version and load_plan restores it."""
    failed = 0
    storage = _FakeStorage()
    my_predbat.components = _FakeComponents(storage)
    my_predbat.plan_version = 7
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    my_predbat.plan_last_updated_minutes = my_predbat.minutes_now
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []

    my_predbat.save_plan()
    payload = storage.saved.get(("predbat", "plan"))
    failed = _check(failed, payload is not None, "plan version persistence: save_plan wrote a payload")
    failed = _check(failed, payload and payload.get("plan_version") == 7, "plan version persistence: payload carries version 7")

    my_predbat.plan_version = 0
    my_predbat.plan_valid = False
    my_predbat.load_plan()
    failed = _check(failed, my_predbat.plan_version == 7, "plan version persistence: load_plan restored version 7")
    failed = _check(failed, my_predbat.plan_valid, "plan version persistence: load_plan marked plan valid")
    my_predbat.components = None
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
        failed += _scenario_plan_once_recompute(my_predbat)
        failed += _scenario_plan_once_reuse(my_predbat)
        failed += _scenario_plan_once_unscheduled_forces(my_predbat)
        failed += _scenario_plan_once_failures(my_predbat)
        failed += _scenario_execute_once(my_predbat)
        failed += _scenario_plan_version_bump(my_predbat)
        failed += _scenario_plan_version_persistence(my_predbat)
    finally:
        _restore_state(my_predbat, saved)
    return failed
