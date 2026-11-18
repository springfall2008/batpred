# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests for watch_list handling of Home Assistant event entities (issue #6105).

An HA event entity's state is the timestamp of its last fire, so it changes on
every refire even when the event data has not changed. Predbat used to treat
every such refire as a change and invalidate the plan, so an Octopus free
electricity session event in watch_list forced a pointless full replan every
~15 minutes. The fixture below replays the three refires from the log attached
to #6102 (05:11:14, 05:26:14, 05:41:13 on 10 Nov 2026): the `events` attribute
- 38 past sessions, the newest starting 1 Nov 2026, none upcoming - is
identical in all three, only the state timestamp moves.

The fix keys the change test in trigger_watch_list() on a fingerprint stored
per watched entity: for event entities the fingerprint is a hash of the
attributes, ignoring the state; for every other entity it is the state,
exactly as before.
"""

from tests.test_infra import run_async

EVENT_ENTITY = "event.octopus_energy_test_octoplus_free_electricity_session_events"


def _free_electricity_events(extra_sessions=None):
    """
    Build the 38-session `events` attribute from the #6102 log, newest first.

    Sessions run through October 2026 with the newest starting 1 Nov 2026,
    matching the log's "38 past sessions, the newest on 1 Nov 2026, none
    upcoming". The exact times do not matter, only that two builds agree.
    """
    sessions = []
    for index in range(38):
        day = 1 + (index % 28)
        month = 11 if index == 0 else 10
        sessions.append({"start": "2026-{:02d}-{:02d}T00:30:00+00:00".format(month, day), "end": "2026-{:02d}-{:02d}T01:30:00+00:00".format(month, day), "rate": 0.0})
    if extra_sessions:
        sessions.extend(extra_sessions)
    return sessions


def _event_state(state, events):
    """
    Build an HA state dict for the event entity as the websocket path sees it.
    """
    return {"entity_id": EVENT_ENTITY, "state": state, "attributes": {"events": events, "friendly_name": "Octoplus Free Electricity Session Events"}}


def _fire(my_predbat, entity_id, attribute, old, new):
    """
    Fire one watch_list trigger and return True if it invalidated the plan.

    The plan is reset to valid before each fire so the return value measures
    this fire alone, not an earlier one.
    """
    my_predbat.plan_valid = True
    my_predbat.update_pending = False
    run_async(my_predbat.trigger_watch_list(entity_id, attribute, old, new))
    return (not my_predbat.plan_valid) and my_predbat.update_pending


def test_watch_list_event_identical_refire(my_predbat=None):
    """
    Replay the three #6102 refires: only the first may invalidate the plan.
    """
    print("\n=== Testing watch_list identical event refire does not replan (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}
    events = _free_electricity_events()
    states = ["2026-11-10T05:11:14.032+00:00", "2026-11-10T05:26:14.118+00:00", "2026-11-10T05:41:13.955+00:00"]
    triggered = 0
    previous = None
    for state in states:
        new = _event_state(state, events)
        if _fire(my_predbat, EVENT_ENTITY, None, previous, new):
            triggered += 1
        previous = new

    if triggered != 1:
        print("ERROR: Expected 1 replan from 3 identical refires, got {}".format(triggered))
        failed += 1
    else:
        print("✓ 3 identical event refires caused only 1 replan (the first sight)")
    return failed


def test_watch_list_event_changed_events(my_predbat=None):
    """
    A changed `events` payload must still invalidate the plan.
    """
    print("\n=== Testing watch_list changed event payload replans (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}
    events = _free_electricity_events()
    first = _event_state("2026-11-10T05:11:14.032+00:00", events)
    second = _event_state("2026-11-10T05:26:14.118+00:00", events)
    changed_events = _free_electricity_events(extra_sessions=[{"start": "2026-11-10T12:00:00+00:00", "end": "2026-11-10T13:00:00+00:00", "rate": 0.0}])
    third = _event_state("2026-11-10T05:41:13.955+00:00", changed_events)
    fourth = _event_state("2026-11-10T05:56:14.201+00:00", changed_events)

    triggered = 0
    if _fire(my_predbat, EVENT_ENTITY, None, None, first):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, None, first, second):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, None, second, third):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, None, third, fourth):
        triggered += 1

    if triggered != 2:
        print("ERROR: Expected 2 replans (first sight + changed payload), got {}".format(triggered))
        failed += 1
    else:
        print("✓ Changed event payload replanned, identical refires did not")
    return failed


def test_watch_list_sensor_state_change(my_predbat=None):
    """
    Non-event entities keep the old behaviour: state change replans, an
    attribute-only change does not.
    """
    print("\n=== Testing watch_list sensor state change still replans (#6105) ===")
    failed = 0

    entity_id = "sensor.test_watch_sensor"
    my_predbat.watch_list = [entity_id]
    my_predbat.watch_list_fingerprints = {}

    triggered = 0
    if _fire(my_predbat, entity_id, None, {"entity_id": entity_id, "state": 100, "attributes": {}}, {"entity_id": entity_id, "state": 200, "attributes": {"unit": "W"}}):
        triggered += 1
    if _fire(my_predbat, entity_id, None, {"entity_id": entity_id, "state": 200, "attributes": {"unit": "W"}}, {"entity_id": entity_id, "state": 200, "attributes": {"unit": "W", "extra": "changed"}}):
        triggered += 1
    if _fire(my_predbat, entity_id, None, {"entity_id": entity_id, "state": 200, "attributes": {"unit": "W"}}, {"entity_id": entity_id, "state": 201, "attributes": {"unit": "W"}}):
        triggered += 1

    if triggered != 2:
        print("ERROR: Expected 2 replans (first sight + state change), got {}".format(triggered))
        failed += 1
    else:
        print("✓ Sensor state change replanned, attribute-only change did not")
    return failed


def test_watch_list_event_loopback_shape(my_predbat=None):
    """
    The loopback path passes the attributes dict as `attribute` instead of a
    name; identical refires through that shape must still be ignored.
    """
    print("\n=== Testing watch_list event refire via loopback call shape (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}
    events = _free_electricity_events()
    first = _event_state("2026-11-10T05:11:14.032+00:00", events)
    second = _event_state("2026-11-10T05:26:14.118+00:00", events)

    triggered = 0
    if _fire(my_predbat, EVENT_ENTITY, first["attributes"], None, first):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, second["attributes"], first, second):
        triggered += 1

    if triggered != 1:
        print("ERROR: Expected 1 replan from 2 identical loopback refires, got {}".format(triggered))
        failed += 1
    else:
        print("✓ Identical event refire via loopback shape caused only 1 replan")
    return failed


def test_watch_list_event_missing_attributes(my_predbat=None):
    """
    An event entity with no attributes must not crash and must still deduplicate.
    """
    print("\n=== Testing watch_list event with no attributes (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}

    triggered = 0
    if _fire(my_predbat, EVENT_ENTITY, None, None, {"entity_id": EVENT_ENTITY, "state": "2026-11-10T05:11:14.032+00:00"}):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, None, None, {"entity_id": EVENT_ENTITY, "state": "2026-11-10T05:26:14.118+00:00"}):
        triggered += 1

    if triggered != 1:
        print("ERROR: Expected 1 replan from 2 attribute-less refires, got {}".format(triggered))
        failed += 1
    else:
        print("✓ Attribute-less event refire caused only 1 replan")
    return failed


def test_watch_list_event_raw_new_value(my_predbat=None):
    """
    A non-dict new value for an event entity is fingerprinted as itself,
    so a change is never silently swallowed.
    """
    print("\n=== Testing watch_list event with raw new value (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}

    triggered = 0
    if _fire(my_predbat, EVENT_ENTITY, None, None, "value-one"):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, None, None, "value-two"):
        triggered += 1
    if _fire(my_predbat, EVENT_ENTITY, None, None, "value-two"):
        triggered += 1

    if triggered != 2:
        print("ERROR: Expected 2 replans (first sight + changed raw value), got {}".format(triggered))
        failed += 1
    else:
        print("✓ Raw event value change replanned, repeat did not")
    return failed


def test_watch_list_event_survives_config_reload(my_predbat=None):
    """
    Fingerprints survive load_user_config(): it runs on every update cycle,
    so resetting them there would make every refire look like a first sight.
    """
    print("\n=== Testing watch_list fingerprints survive config reload (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}
    events = _free_electricity_events()
    first = _event_state("2026-11-10T05:11:14.032+00:00", events)
    second = _event_state("2026-11-10T05:26:14.118+00:00", events)

    triggered = 0
    if _fire(my_predbat, EVENT_ENTITY, None, None, first):
        triggered += 1
    my_predbat.load_user_config()
    if EVENT_ENTITY not in my_predbat.watch_list:
        print("ERROR: load_user_config() dropped the watched entity from watch_list")
        failed += 1
    if _fire(my_predbat, EVENT_ENTITY, None, first, second):
        triggered += 1

    if triggered != 1:
        print("ERROR: Expected 1 replan (first sight only, reload must not reset fingerprints), got {}".format(triggered))
        failed += 1
    else:
        print("✓ Identical refire after config reload did not replan")
    return failed


def test_watch_list_unwatched_entity(my_predbat=None):
    """
    An entity that is not in watch_list never invalidates the plan.
    """
    print("\n=== Testing unwatched entity never triggers watch_list (#6105) ===")
    failed = 0

    my_predbat.watch_list = [EVENT_ENTITY]
    my_predbat.watch_list_fingerprints = {}
    if _fire(my_predbat, "sensor.not_watched", None, None, {"entity_id": "sensor.not_watched", "state": 200, "attributes": {}}):
        print("ERROR: Unwatched entity invalidated the plan")
        failed += 1
    else:
        print("✓ Unwatched entity did not invalidate the plan")
    return failed


def run_watch_list_event_tests(my_predbat):
    """Run all watch_list event entity tests (issue #6105)"""
    print("\n" + "=" * 80)
    print("Watch List Event Tests (#6105)")
    print("=" * 80)

    failed = 0
    failed += test_watch_list_event_identical_refire(my_predbat)
    failed += test_watch_list_event_changed_events(my_predbat)
    failed += test_watch_list_event_loopback_shape(my_predbat)
    failed += test_watch_list_event_missing_attributes(my_predbat)
    failed += test_watch_list_event_raw_new_value(my_predbat)
    failed += test_watch_list_event_survives_config_reload(my_predbat)
    failed += test_watch_list_sensor_state_change(my_predbat)
    failed += test_watch_list_unwatched_entity(my_predbat)
    return failed
