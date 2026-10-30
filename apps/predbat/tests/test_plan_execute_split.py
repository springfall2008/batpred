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

import itertools
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Gives each recompute of the fake calculate_plan a plan no earlier one had
_plan_marker = itertools.count(1)

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
    "plan_preclip",
    "plan_made_by_version",
    "plan_version_content",
    "local_tz",
    "now_utc_real",
    "now_utc",
    "midnight_utc",
    "difference_minutes",
    "minutes_now",
    "minutes_to_midnight",
]


def _save_state(my_predbat):
    """Snapshot the shared fixture attributes this module mutates, so they can be restored."""
    saved = {name: getattr(my_predbat, name, None) for name in _STATE_ATTRS}
    saved["_template_arg"] = my_predbat.args.get("template", None)
    saved["_plan_random_delay_arg"] = my_predbat.args.get("plan_random_delay", None)
    saved["_clock_skew_arg"] = my_predbat.args.get("clock_skew", None)
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
    if saved["_clock_skew_arg"] is None:
        my_predbat.args.pop("clock_skew", None)
    else:
        my_predbat.args["clock_skew"] = saved["_clock_skew_arg"]


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


def _patch_pipeline(my_predbat, stack, inverter_ok=True, plan_valid_after=True, real_time=False, same_plan=False, drop_head=False):
    """
    Patch the fetch/plan/execute pipeline on my_predbat, returning a dict of the created mocks. real_time=True leaves update_time unpatched.

    A recompute makes a plan unlike any before it, unless same_plan=True. drop_head=True removes the head charge
    window as the real calculate_plan does once that window has ended, recompute or not.
    """
    mocks = {}
    names = ("save_current_config",) if real_time else ("update_time", "save_current_config")
    for name in names + ("download_predbat_releases", "fetch_config_options", "publish_rate_and_threshold", "save_plan"):
        mocks[name] = stack.enter_context(patch.object(my_predbat, name))
    mocks["fetch_sensor_data"] = stack.enter_context(patch.object(my_predbat, "fetch_sensor_data", return_value=False))
    mocks["dynamic_load"] = stack.enter_context(patch.object(my_predbat, "dynamic_load", return_value=False))
    mocks["fetch_inverter_data"] = stack.enter_context(patch.object(my_predbat, "fetch_inverter_data", return_value=inverter_ok))

    def fake_calculate_plan(recompute=True, debug_mode=False, publish=True):
        """Stand-in for calculate_plan that marks the plan valid when a recompute is requested."""
        if drop_head and my_predbat.charge_window_best:
            del my_predbat.charge_window_best[0]
            del my_predbat.charge_limit_best[0]
        if recompute and plan_valid_after:
            if not same_plan:
                marker = next(_plan_marker)
                my_predbat.charge_window_best = [{"start": 0, "end": 30, "average": float(marker)}, {"start": 60, "end": 90, "average": float(marker)}]
                my_predbat.charge_limit_best = [50.0, 100.0]
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
    my_predbat.plan_version_content = my_predbat.plan_content()
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
    """Characterise: an aged valid plan executes first, then recomputes and executes again (and persists a new plan version)."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc - timedelta(minutes=9)
    version_before = my_predbat.plan_version
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
    failed = _check(failed, mocks["save_plan"].call_count == 1, "aged plan: new plan persisted")
    failed = _check(failed, my_predbat.plan_version == version_before + 1, "aged plan: version advances once")
    return failed


def _scenario_fused_aged_plan_unchanged(my_predbat):
    """An aged plan that recomputes to exactly the plan already in force is neither re-versioned nor re-saved."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc - timedelta(minutes=9)
    my_predbat.plan_version_content = my_predbat.plan_content()
    version_before = my_predbat.plan_version
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, same_plan=True)
        my_predbat.update_pred(scheduled=True)
    failed = _check(failed, mocks["calculate_plan"].call_count == 2, "aged plan unchanged: still recomputed")
    failed = _check(failed, mocks["execute_plan"].call_count == 2, "aged plan unchanged: executed twice")
    failed = _check(failed, mocks["save_plan"].call_count == 0, "aged plan unchanged: save_plan not called")
    failed = _check(failed, my_predbat.plan_version == version_before, "aged plan unchanged: version not advanced")
    return failed


def _scenario_head_window_dropped(my_predbat):
    """A plan reused as time moves on is re-versioned and re-saved once its head window drops, and not again while it stays the same."""
    failed = 0
    my_predbat.plan_valid = True
    my_predbat.plan_last_updated = my_predbat.now_utc
    my_predbat.charge_window_best = [{"start": 0, "end": 30, "average": 5.0}, {"start": 60, "end": 90, "average": 5.0}]
    my_predbat.charge_limit_best = [50.0, 100.0]
    my_predbat.plan_version_content = my_predbat.plan_content()
    version_before = my_predbat.plan_version
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, drop_head=True)
        artifact = my_predbat.plan_once(scheduled=True)
    failed = _check(failed, artifact and artifact.get("recomputed") is False, "head dropped: plan reused, not recomputed")
    failed = _check(failed, len(my_predbat.charge_window_best) == 1, "head dropped: head window removed")
    failed = _check(failed, mocks["save_plan"].call_count == 1, "head dropped: save_plan called once")
    failed = _check(failed, my_predbat.plan_version == version_before + 1, "head dropped: version advances once")
    failed = _check(failed, artifact and artifact.get("plan_version") == version_before + 1, "head dropped: artifact carries the new version")

    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.plan_once(scheduled=True)
    failed = _check(failed, mocks["save_plan"].call_count == 0, "head dropped: nothing changed on the next cycle, save_plan not called")
    failed = _check(failed, my_predbat.plan_version == version_before + 1, "head dropped: version unchanged on the next cycle")
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
    my_predbat.plan_version_content = my_predbat.plan_content()
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
        self.expiry = {}

    async def save(self, namespace, key, data, format="json", expiry=None):
        """Record the payload and its expiry under (namespace, key)."""
        self.saved[(namespace, key)] = data
        self.expiry[(namespace, key)] = expiry

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

    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, same_plan=True)
        my_predbat.plan_once(scheduled=False)
    failed = _check(failed, my_predbat.plan_version == 2, "plan version: unchanged when a forced recompute makes the same plan")
    failed = _check(failed, mocks["save_plan"].call_count == 0, "plan version: identical plan not re-saved")
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


def _scenario_stale_plan_restored(my_predbat):
    """
    The saved plan never expires: load_plan restores it however old it is and becomes the baseline a change is
    measured from. One made on an earlier day is restored invalid, as its window minutes count from that day's
    midnight, so the first run recomputes before anything executes it.
    """
    failed = 0
    storage = _FakeStorage()
    my_predbat.components = _FakeComponents(storage)
    now = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    windows = [{"start": 1260, "end": 1290, "average": 5.0}]

    def save_at(saved_at, version):
        """Save a one-window plan stamped at saved_at."""
        my_predbat.now_utc = saved_at
        my_predbat.plan_last_updated = saved_at
        my_predbat.plan_last_updated_minutes = saved_at.hour * 60 + saved_at.minute
        my_predbat.plan_version = version
        my_predbat.charge_window_best = [dict(window) for window in windows]
        my_predbat.charge_limit_best = [80.0]
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        my_predbat.plan_valid = True
        my_predbat.save_plan()

    def load_at(load_time):
        """Clear the plan, then restore it with the clock at load_time."""
        my_predbat.now_utc = load_time
        my_predbat.plan_valid = False
        my_predbat.plan_version = 0
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.plan_version_content = None
        my_predbat.load_plan()

    save_at(now - timedelta(hours=10), 3)
    failed = _check(failed, storage.expiry.get(("predbat", "plan")) is None, "stale plan: saved without an expiry")

    load_at(now)
    failed = _check(failed, my_predbat.charge_window_best == windows, "stale plan: a plan 10 hours old is restored")
    failed = _check(failed, my_predbat.plan_version == 3, "stale plan: its version is restored")
    failed = _check(failed, my_predbat.plan_valid, "stale plan: a plan from earlier today is restored valid")
    failed = _check(failed, my_predbat.plan_version_content == my_predbat.plan_content(), "stale plan: the restored plan is the change baseline")

    save_at(now - timedelta(days=3), 4)
    load_at(now)
    failed = _check(failed, my_predbat.charge_window_best == windows, "stale plan: a plan 3 days old is restored")
    failed = _check(failed, my_predbat.plan_version == 4, "stale plan: the old plan's version is restored")
    failed = _check(failed, not my_predbat.plan_valid, "stale plan: a plan from an earlier day is restored invalid, to recompute before executing")
    my_predbat.components = None
    return failed


def _scenario_plan_at_default(my_predbat):
    """plan_once without plan_at plans at the wall clock, exactly as before."""
    failed = 0
    my_predbat.plan_valid = False
    my_predbat.plan_last_updated = None
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        my_predbat.plan_once(scheduled=True)
    failed = _check(failed, mocks["update_time"].call_args.kwargs.get("now") is None, "plan_at default: update_time given no instant")

    my_predbat.args.pop("clock_skew", None)
    my_predbat.plan_valid = False
    my_predbat.plan_last_updated = None
    before = datetime.now(timezone.utc)
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        artifact = my_predbat.plan_once(scheduled=True)
    after = datetime.now(timezone.utc)
    failed = _check(failed, before <= my_predbat.now_utc_real <= after, "plan_at default: now_utc_real read from the wall clock")
    failed = _check(failed, before - timedelta(minutes=5) < my_predbat.now_utc <= after, "plan_at default: now_utc is the wall clock rounded down")
    failed = _check(failed, my_predbat.now_utc.minute % 5 == 0 and my_predbat.now_utc.second == 0, "plan_at default: now_utc on a 5 minute boundary")
    failed = _check(failed, artifact and artifact.get("plan_last_updated") == my_predbat.now_utc, "plan_at default: plan stamped at now_utc")
    return failed


def _scenario_plan_at_drives_time(my_predbat):
    """plan_at on a boundary sets now_utc, midnight_utc and minutes_now for the run, in the configured timezone."""
    failed = 0
    my_predbat.args.pop("clock_skew", None)
    my_predbat.plan_valid = False
    my_predbat.plan_last_updated = None
    plan_at = datetime(2026, 6, 15, 11, 35, tzinfo=timezone.utc)  # 12:35 BST
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        artifact = my_predbat.plan_once(scheduled=True, plan_at=plan_at)
    failed = _check(failed, my_predbat.now_utc == plan_at, "plan_at: now_utc equals plan_at")
    failed = _check(failed, my_predbat.now_utc_real == plan_at, "plan_at: now_utc_real equals plan_at")
    failed = _check(failed, getattr(my_predbat.now_utc.tzinfo, "zone", None) == my_predbat.local_tz.zone, "plan_at: now_utc carries the configured timezone")
    failed = _check(failed, my_predbat.now_utc.hour == 12, "plan_at: now_utc is 12:35 local (BST)")
    failed = _check(failed, my_predbat.minutes_now == 12 * 60 + 35, "plan_at: minutes_now is 755")
    failed = _check(failed, my_predbat.minutes_to_midnight == 24 * 60 - 755, "plan_at: minutes_to_midnight follows")
    failed = _check(failed, my_predbat.midnight_utc == datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc), "plan_at: midnight_utc is local midnight of plan_at")
    failed = _check(failed, artifact and artifact.get("plan_last_updated") == plan_at, "plan_at: plan stamped at plan_at")
    return failed


def _scenario_plan_at_rounding(my_predbat):
    """A plan_at off the boundary is rounded down the same way the clock is; now_utc_real keeps the exact instant."""
    failed = 0
    my_predbat.args.pop("clock_skew", None)
    plan_at = datetime(2026, 1, 10, 23, 59, 42, 500000, tzinfo=timezone.utc)  # GMT, so local == UTC
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        my_predbat.plan_once(scheduled=False, plan_at=plan_at)
    failed = _check(failed, my_predbat.now_utc == datetime(2026, 1, 10, 23, 55, tzinfo=timezone.utc), "plan_at rounding: now_utc rounded down to 23:55")
    failed = _check(failed, my_predbat.now_utc_real == plan_at, "plan_at rounding: now_utc_real keeps the exact instant")
    failed = _check(failed, my_predbat.minutes_now == 23 * 60 + 55, "plan_at rounding: minutes_now is 1435")
    failed = _check(failed, my_predbat.midnight_utc == datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc), "plan_at rounding: midnight_utc is the same local day")
    return failed


def _scenario_plan_at_ignores_skew(my_predbat):
    """clock_skew offsets the wall clock only; a caller-chosen plan_at is used as given."""
    failed = 0
    my_predbat.args["clock_skew"] = 10
    plan_at = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        my_predbat.plan_once(scheduled=False, plan_at=plan_at)
    my_predbat.args.pop("clock_skew", None)
    failed = _check(failed, my_predbat.now_utc == plan_at, "plan_at skew: clock_skew not applied to plan_at")
    return failed


def _scenario_plan_at_ahead_of_clock(my_predbat):
    """A plan stamped at the next boundary (plan_at ahead of the wall clock) is fresh to a wall-clock update_pred, not a day old."""
    failed = 0
    my_predbat.args.pop("clock_skew", None)
    now = datetime.now(timezone.utc)
    next_boundary = now.replace(second=0, microsecond=0, minute=now.minute - (now.minute % 5)) + timedelta(minutes=5)
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        my_predbat.plan_once(scheduled=False, plan_at=next_boundary)
    failed = _check(failed, my_predbat.plan_last_updated == next_boundary, "plan_at ahead: plan stamped at the next boundary")
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, real_time=True)
        my_predbat.update_pred(scheduled=True)
    failed = _check(failed, mocks["calculate_plan"].call_count == 1, "plan_at ahead: calculate_plan called once (negative age is not an aged plan)")
    failed = _check(failed, mocks["calculate_plan"].call_args.kwargs.get("recompute") is False, "plan_at ahead: plan reused, not recomputed")
    failed = _check(failed, mocks["save_plan"].call_count == 0, "plan_at ahead: save_plan not called")
    failed = _check(failed, mocks["execute_plan"].call_count == 1, "plan_at ahead: execute_plan called once")
    return failed


def _scenario_plan_at_naive_rejected(my_predbat):
    """A naive plan_at raises ValueError before the run touches any state."""
    failed = 0
    raised = False
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        try:
            my_predbat.plan_once(scheduled=True, plan_at=datetime(2026, 1, 10, 9, 0))
        except ValueError:
            raised = True
    failed = _check(failed, raised, "plan_at naive: ValueError raised")
    failed = _check(failed, mocks["update_time"].call_count == 0, "plan_at naive: update_time not called")
    failed = _check(failed, mocks["calculate_plan"].call_count == 0, "plan_at naive: calculate_plan not called")

    raised = False
    try:
        my_predbat.update_time(now=datetime(2026, 1, 10, 9, 0))
    except ValueError:
        raised = True
    failed = _check(failed, raised, "plan_at naive: update_time(now=naive) raises ValueError")
    return failed


def _scenario_update_pred_plan_at_stamps_plan(my_predbat):
    """test_update_pred_plan_at_stamps_plan: a forced update_pred at plan_at stamps the plan at that instant."""
    failed = 0
    my_predbat.args.pop("clock_skew", None)
    plan_at = datetime(2026, 6, 15, 11, 35, tzinfo=timezone.utc)
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        my_predbat.update_pred(scheduled=False, plan_at=plan_at)
    failed = _check(failed, my_predbat.plan_last_updated == plan_at, "test_update_pred_plan_at_stamps_plan: plan_last_updated equals plan_at")
    failed = _check(failed, my_predbat.now_utc == plan_at, "test_update_pred_plan_at_stamps_plan: now_utc equals plan_at")
    return failed


def _update_pred_after_forced_run(my_predbat, minutes_later):
    """Force a run at a fixed instant, then run a scheduled update_pred minutes_later; returns the mocks, the version before it and the log lines."""
    my_predbat.args.pop("clock_skew", None)
    my_predbat.calculate_plan_every = 30
    plan_at = datetime(2026, 6, 15, 11, 35, tzinfo=timezone.utc)
    with ExitStack() as stack:
        _patch_pipeline(my_predbat, stack, real_time=True)
        my_predbat.update_pred(scheduled=False, plan_at=plan_at)
    version_before = my_predbat.plan_version
    logged = []
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack, real_time=True)
        stack.enter_context(patch.object(my_predbat, "log", side_effect=lambda message, *args, **kwargs: logged.append(str(message))))
        my_predbat.update_pred(scheduled=True, plan_at=plan_at + timedelta(minutes=minutes_later))
    return mocks, version_before, logged


def _scenario_update_pred_scheduled_reuses_fresh_plan(my_predbat):
    """test_update_pred_scheduled_reuses_fresh_plan: a 5 minute old plan is not recomputed."""
    failed = 0
    mocks, version_before, logged = _update_pred_after_forced_run(my_predbat, 5)
    failed = _check(failed, my_predbat.plan_version == version_before, "test_update_pred_scheduled_reuses_fresh_plan: plan_version unchanged")
    failed = _check(failed, any("Will not recompute the plan" in line for line in logged), "test_update_pred_scheduled_reuses_fresh_plan: 'Will not recompute the plan' logged")
    failed = _check(failed, all(call.kwargs.get("recompute") is False for call in mocks["calculate_plan"].call_args_list), "test_update_pred_scheduled_reuses_fresh_plan: calculate_plan never asked to recompute")
    return failed


def _scenario_update_pred_scheduled_recomputes_old_plan(my_predbat):
    """test_update_pred_scheduled_recomputes_old_plan: a 30 minute old plan is recomputed at plan_at."""
    failed = 0
    mocks, version_before, _logged = _update_pred_after_forced_run(my_predbat, 30)
    failed = _check(failed, my_predbat.plan_version == version_before + 1, "test_update_pred_scheduled_recomputes_old_plan: plan_version advanced")
    failed = _check(failed, any(call.kwargs.get("recompute") is True for call in mocks["calculate_plan"].call_args_list), "test_update_pred_scheduled_recomputes_old_plan: calculate_plan asked to recompute")
    failed = _check(failed, my_predbat.plan_last_updated == datetime(2026, 6, 15, 12, 5, tzinfo=timezone.utc), "test_update_pred_scheduled_recomputes_old_plan: plan stamped at plan_at")
    return failed


def _scenario_update_pred_rejects_naive_plan_at(my_predbat):
    """test_update_pred_rejects_naive_plan_at: a naive plan_at raises ValueError as plan_once does."""
    failed = 0
    raised = False
    with ExitStack() as stack:
        mocks = _patch_pipeline(my_predbat, stack)
        try:
            my_predbat.update_pred(scheduled=True, plan_at=datetime(2026, 1, 10, 9, 0))
        except ValueError:
            raised = True
    failed = _check(failed, raised, "test_update_pred_rejects_naive_plan_at: ValueError raised")
    failed = _check(failed, mocks["execute_plan"].call_count == 0, "test_update_pred_rejects_naive_plan_at: nothing executed")
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
        failed += _scenario_fused_aged_plan_unchanged(my_predbat)
        failed += _scenario_head_window_dropped(my_predbat)
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
        failed += _scenario_stale_plan_restored(my_predbat)
        failed += _scenario_plan_at_default(my_predbat)
        failed += _scenario_plan_at_drives_time(my_predbat)
        failed += _scenario_plan_at_rounding(my_predbat)
        failed += _scenario_plan_at_ignores_skew(my_predbat)
        failed += _scenario_plan_at_ahead_of_clock(my_predbat)
        failed += _scenario_plan_at_naive_rejected(my_predbat)
        failed += _scenario_update_pred_plan_at_stamps_plan(my_predbat)
        failed += _scenario_update_pred_scheduled_reuses_fresh_plan(my_predbat)
        failed += _scenario_update_pred_scheduled_recomputes_old_plan(my_predbat)
        failed += _scenario_update_pred_rejects_naive_plan_at(my_predbat)
    finally:
        _restore_state(my_predbat, saved)
    return failed
