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
History aliases: an entity that takes over from another under a new id reads the replaced entity's
history for the time before its own history starts (PredBat.set_history_alias).

History is fetched by entity id, so renaming the entity an energy arg points at would otherwise
restart its history from zero - the load forecast, PV calibration and the ML load model then work
from no days at all until the history builds back up.
"""

from datetime import datetime, timedelta, timezone

from fetch import Fetch
from predbat import PredBat
from utils import get_now_from_cumulative, prepend_older_history

NOW = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DAILY_KWH = 12.0
SITE_LOAD = "sensor.predbat_gateway_load_today"
SITE_PV = "sensor.predbat_gateway_pv_today"
SERIAL_LOAD = "sensor.predbat_gateway_14g318_load_today"
SERIAL_PV = "sensor.predbat_gateway_14g318_pv_today"
LOAD_POWER = "sensor.predbat_gateway_14g318_load_power"


def _record(when, state):
    """One history record in the shape get_history returns."""
    return {"state": str(state), "last_updated": when.strftime(TIME_FORMAT), "attributes": {"unit_of_measurement": "kWh"}}


def _counter_history(hours_back, step_minutes=5):
    """A *_today counter sampled every step_minutes, rising DAILY_KWH a day and resetting at midnight, oldest first."""
    records = []
    when = NOW - timedelta(hours=hours_back)
    while when <= NOW:
        midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
        records.append(_record(when, round(DAILY_KWH * (when - midnight).total_seconds() / 86400.0, 3)))
        when += timedelta(minutes=step_minutes)
    return records


def _power_history(hours_back):
    """A steady load power sensor sampled every 5 minutes, oldest first."""
    records = []
    when = NOW - timedelta(hours=hours_back)
    while when <= NOW:
        records.append({"state": str(DAILY_KWH * 1000 / 24), "last_updated": when.strftime(TIME_FORMAT), "attributes": {"unit_of_measurement": "W"}})
        when += timedelta(minutes=5)
    return records


class FakeHistorySource:
    """Stands in for the HA interface (and optionally the history cache), recording every history request."""

    def __init__(self, histories):
        """histories maps entity id to its records, oldest first; a missing entity returns None."""
        self.histories = histories
        self.calls = []

    def get_history(self, entity_id, days=30, now=None, tracked=None):
        """Return a copy of the entity's records wrapped as get_history does, or None when it has none."""
        self.calls.append((entity_id, days, tracked))
        records = self.histories.get(entity_id)
        if records is None:
            return None
        return [list(records)]


class FakeComponents:
    """Components registry exposing only a history cache."""

    def __init__(self, cache):
        """cache is returned for the ha_history component."""
        self.cache = cache

    def get_component(self, name):
        """Return the history cache for ha_history, None for anything else."""
        return self.cache if name == "ha_history" else None


class HistoryBase:
    """The parts of PredBat that get_history_wrapper and set_history_alias use, with the real methods."""

    set_history_alias = PredBat.set_history_alias
    get_history_wrapper = PredBat.get_history_wrapper

    def __init__(self, histories, use_cache=False):
        """Serve histories through a fake HA interface, or through a fake history cache when use_cache is set."""
        self.source = FakeHistorySource(histories)
        self.ha_interface = self.source
        self.components = FakeComponents(self.source) if use_cache else None
        self.now_utc = NOW
        self.history_aliases = {}
        self.logs = []

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Every entity here is an energy counter in kWh."""
        return "kWh" if attribute == "unit_of_measurement" else default

    def log(self, message):
        """Keep log lines for inspection."""
        self.logs.append(message)


class CutoverSim(HistoryBase, Fetch):
    """Runs PredBat's real load/PV history fetch against fake recorder history (the reviewer's cut-over simulation)."""

    def __init__(self, histories, load_today, pv_today):
        """Point load_today / pv_today at the given entities, with 7 days of load power history."""
        HistoryBase.__init__(self, histories)
        self.plan_interval_minutes = 30
        self.args = {"load_today": [load_today], "pv_today": [pv_today], "load_power": [LOAD_POWER]}
        self.days_previous = [1, 2, 3, 4, 5, 6, 7]
        self.days_previous_weight = [1] * 7
        self.max_days_previous = 8
        self.base_load = 0
        self.car_charging_hold = False
        self.car_charging_energy = {}
        self.iboost_energy_subtract = False
        self.iboost_energy_today = {}

    def get_arg(self, key, default=None, indirect=True, **kwargs):
        """Return a configured arg."""
        return self.args.get(key, default)

    def record_status(self, *args, **kwargs):
        """Status is not under test."""

    def metrics(self):
        """Return the history-derived figures the planner starts from."""
        load_minutes, load_minutes_age = self.minute_data_load(NOW, "load_today", self.max_days_previous, required_unit="kWh", load_scaling=1.0, interpolate=True)
        pv = self.minute_data_import_export(self.max_days_previous, NOW, "pv_today", required_unit="kWh")
        minutes_now = NOW.hour * 60 + NOW.minute
        return {
            "load_minutes_age": load_minutes_age,
            "pv_yesterday": (pv.get(24 * 60, 0) - pv.get(48 * 60, 0)) if pv else 0,
            "load_today_so_far": get_now_from_cumulative(load_minutes, minutes_now, backwards=True),
            "pv_today_so_far": get_now_from_cumulative(pv, minutes_now, backwards=True),
        }


def _check(failed, condition, message):
    """Print and record a failed check."""
    if not condition:
        print("ERROR: " + message)
        return True
    return failed


def test_prepend_older_history():
    """prepend_older_history keeps only the older records from before the newer series starts."""
    failed = False
    older = _counter_history(48)
    newer = _counter_history(2)
    merged = prepend_older_history(newer, older)
    cutoff = newer[0]["last_updated"]
    failed = _check(failed, merged[len(merged) - len(newer) :] == newer, "the newer records must be kept whole, at the end")
    failed = _check(failed, all(record["last_updated"] < cutoff for record in merged[: len(merged) - len(newer)]), "only older records from before the cutoff may be prepended")
    failed = _check(failed, len(merged) == len(older), "a newer series sampled like the older one should replace exactly its overlap")
    failed = _check(failed, prepend_older_history([], older) == older, "with no newer records the older records are used whole")
    failed = _check(failed, prepend_older_history(newer, []) == newer, "with no older records the newer records are unchanged")
    failed = _check(failed, prepend_older_history(newer, None) == newer, "a missing older history leaves the newer records unchanged")
    broken = [{"state": "1.0", "last_updated": "not a time"}] + older
    failed = _check(failed, prepend_older_history(newer, broken) == merged, "an older record with an unparsable time is skipped")
    bad_newer = [{"state": "1.0", "last_updated": "not a time"}]
    failed = _check(failed, prepend_older_history(bad_newer, older) == bad_newer, "if the newer series start cannot be read nothing is prepended")
    return failed


def test_get_history_wrapper_aliases():
    """get_history_wrapper fills an aliased entity's history in from the entity it replaced."""
    failed = False
    legacy = _counter_history(8 * 24)

    # No alias registered: behaviour is unchanged and the legacy entity is never read.
    base = HistoryBase({SITE_LOAD: _counter_history(2), SERIAL_LOAD: legacy})
    history = base.get_history_wrapper(SITE_LOAD, days=8)
    failed = _check(failed, history == [_counter_history(2)], "without an alias the entity's own history must be returned as is")
    failed = _check(failed, [call[0] for call in base.source.calls] == [SITE_LOAD], "without an alias only the entity itself may be read")

    # New entity with 2 hours of history: the legacy records before those 2 hours are filled in.
    base.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    history = base.get_history_wrapper(SITE_LOAD, days=8)
    expected = prepend_older_history(_counter_history(2), legacy)
    failed = _check(failed, history == [expected], "the legacy history must be filled in before the new entity's records")
    failed = _check(failed, history[0][0]["last_updated"] == legacy[0]["last_updated"], "the merged history must reach back as far as the legacy history")

    # Brand-new entity with no history at all yet (the fetch returns None): the legacy history is used, and required does not raise.
    base = HistoryBase({SERIAL_LOAD: legacy})
    base.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    history = base.get_history_wrapper(SITE_LOAD, days=8)
    failed = _check(failed, history == [legacy], "an entity with no history yet must read the legacy history whole")

    # Once the entity's own history covers the window the legacy entity is not read.
    base = HistoryBase({SITE_LOAD: _counter_history(8 * 24), SERIAL_LOAD: legacy})
    base.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    history = base.get_history_wrapper(SITE_LOAD, days=8)
    failed = _check(failed, history == [_counter_history(8 * 24)], "a fully covered entity must return its own history")
    failed = _check(failed, SERIAL_LOAD not in [call[0] for call in base.source.calls], "a fully covered entity must not read the legacy entity")

    # Neither has history: still a failure, exactly as before.
    base = HistoryBase({})
    base.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    raised = False
    try:
        base.get_history_wrapper(SITE_LOAD, days=8)
    except ValueError:
        raised = True
    failed = _check(failed, raised, "a required history with no data anywhere must still raise")
    failed = _check(failed, base.get_history_wrapper(SITE_LOAD, days=8, required=False) is None, "an optional history with no data anywhere must still be None")

    # Through the history cache the legacy entity is read untracked, so the cache does not keep refreshing it.
    base = HistoryBase({SITE_LOAD: _counter_history(2), SERIAL_LOAD: legacy}, use_cache=True)
    base.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    base.get_history_wrapper(SITE_LOAD, days=8)
    failed = _check(failed, (SITE_LOAD, 8, True) in base.source.calls, "the entity itself is read as the caller asked (tracked)")
    failed = _check(failed, (SERIAL_LOAD, 8, False) in base.source.calls, "the legacy entity must be read untracked")

    # An empty list, or an alias to the entity itself, removes the alias.
    base.set_history_alias(SITE_LOAD, [SITE_LOAD, ""])
    failed = _check(failed, SITE_LOAD not in base.history_aliases, "an alias to itself must not be registered")
    base.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    base.set_history_alias(SITE_LOAD, [])
    failed = _check(failed, SITE_LOAD not in base.history_aliases, "an empty alias list must remove the alias")
    return failed


def test_history_alias_cutover_keeps_planning_history():
    """Re-pointing load_today / pv_today at a 2 hour old entity keeps the history PredBat plans from when aliased.

    Mirrors a site upgrading to site-level energy entities: the old serial-named entities hold 8 days
    of history, the new ones have existed for 2 hours.
    """
    failed = False
    histories = {
        SERIAL_LOAD: _counter_history(8 * 24),
        SERIAL_PV: _counter_history(8 * 24),
        SITE_LOAD: _counter_history(2),
        SITE_PV: _counter_history(2),
        LOAD_POWER: _power_history(7 * 24),
    }

    before = CutoverSim(histories, SERIAL_LOAD, SERIAL_PV).metrics()
    renamed = CutoverSim(histories, SITE_LOAD, SITE_PV).metrics()
    aliased_sim = CutoverSim(histories, SITE_LOAD, SITE_PV)
    aliased_sim.set_history_alias(SITE_LOAD, [SERIAL_LOAD])
    aliased_sim.set_history_alias(SITE_PV, [SERIAL_PV])
    aliased = aliased_sim.metrics()

    print("  serial-named entities: {}".format(before))
    print("  renamed, no alias:     {}".format(renamed))
    print("  renamed, aliased:      {}".format(aliased))

    failed = _check(failed, renamed["load_minutes_age"] == 0 and renamed["pv_yesterday"] == 0, "sanity: renaming without an alias should lose the history (the problem being fixed)")
    failed = _check(failed, before["load_minutes_age"] >= 7, "sanity: the serial-named entity should have a week of load history")
    for key in ("load_minutes_age",):
        failed = _check(failed, aliased[key] == before[key], "{}: aliased {} should match the serial-named {}".format(key, aliased[key], before[key]))
    for key in ("pv_yesterday", "load_today_so_far", "pv_today_so_far"):
        failed = _check(failed, abs(aliased[key] - before[key]) < 0.05, "{}: aliased {:.2f} should match the serial-named {:.2f}".format(key, aliased[key], before[key]))
    return failed


def run_history_alias_tests(my_predbat=None):
    """Run the history alias tests. Returns True on failure."""
    failed = False
    for test in (test_prepend_older_history, test_get_history_wrapper_aliases, test_history_alias_cutover_keeps_planning_history):
        print("**** Running {} ****".format(test.__name__))
        if test():
            print("**** {} FAILED ****".format(test.__name__))
            failed = True
    return failed
