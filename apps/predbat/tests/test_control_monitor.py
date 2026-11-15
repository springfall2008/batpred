# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test control failure monitor
# -----------------------------------------------------------------------------

"""Tests for the control failure monitor.

The expensive failure for this feature is a false alarm - telling a customer their inverter
ignored Predbat when it was ramping up, legitimately limited, or simply not yet reported. Most of
these tests therefore assert SILENCE, and the ones that expect a failure check it fires exactly once.
"""

import json

import pytz

from control_monitor import (
    ControlMonitor,
    judge_reading,
    MODE_CHARGING,
    MODE_EXPORTING,
    MODE_FREEZE_CHARGING,
    MODE_FREEZE_EXPORTING,
    GRACE_S,
    FLOOR_W,
    format_failure,
    DEFAULT_WINDOW_S,
)

# 2023-11-14 00:00:00 UTC, so slot times format as 00:00-01:00 in UTC
T0 = 1699920000.0
SLOT_START = T0
SLOT_END = T0 + 3600


def close(a, b):
    """True when two floats are equal to within rounding."""
    return abs(a - b) < 1e-6


def charging_monitor(target=80.0):
    """A monitor with inverter 0 entering Charging at T0 for the slot T0..T0+1h."""
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START, SLOT_END, target, T0)
    return monitor


def feed(monitor, offsets, power, soc=50.0, charge_w=3000, discharge_w=3000, inverter_id=0):
    """Observe one reading at T0+offset for each offset and return every event that came back."""
    events = []
    for offset in offsets:
        event = monitor.observe(inverter_id, power, soc, charge_w, discharge_w, T0 + offset)
        if event is not None:
            events.append(event)
    return events


def test_judge_reading():
    """Each mode's failing condition, and the readings that cannot be judged at all."""
    failed = False
    cases = [
        # mode, requested, target, power, soc, expected
        (MODE_CHARGING, 3000, 80, 0, 50, True),  # asked to charge, idle
        (MODE_CHARGING, 3000, 80, -749, 50, True),  # just under 25%
        (MODE_CHARGING, 3000, 80, -750, 50, False),  # exactly 25% is passing
        (MODE_CHARGING, 3000, 80, 0, 80, None),  # target reached - not judged
        (MODE_CHARGING, FLOOR_W - 1, 80, 0, 50, None),  # too little requested to judge
        (MODE_CHARGING, None, 80, 0, 50, None),  # nothing written
        (MODE_EXPORTING, 2000, 20, 499, 50, True),
        (MODE_EXPORTING, 2000, 20, 500, 50, False),
        (MODE_EXPORTING, 2000, 20, 0, 20, None),  # at export target - not judged
        (MODE_FREEZE_CHARGING, None, None, FLOOR_W, 50, False),  # at the floor is tolerated
        (MODE_FREEZE_CHARGING, None, None, FLOOR_W + 1, 50, True),  # discharging past the floor
        (MODE_FREEZE_CHARGING, None, None, -3000, 50, False),  # charging is fine during freeze charge
        (MODE_FREEZE_EXPORTING, None, None, -(FLOOR_W + 1), 50, True),  # charging past the floor
        (MODE_FREEZE_EXPORTING, None, None, 3000, 50, False),
        ("Demand", 3000, 80, 0, 50, None),
    ]
    for mode, requested, target, power, soc, expected in cases:
        got = judge_reading(mode, requested, target, power, soc)
        if got is not expected:
            print("ERROR: judge_reading({}, {}, {}, {}, {}) = {}, expected {}".format(mode, requested, target, power, soc, got, expected))
            failed = True
    return failed


def test_nothing_during_grace():
    """Readings inside the grace period are never judged, however bad."""
    monitor = charging_monitor()
    events = feed(monitor, [60, 120, 180, 240], power=0)
    if events or monitor.total != 0 or monitor.tracking[0]["consecutive"] != 0:
        print("ERROR: grace readings were judged: events {} total {} consecutive {}".format(events, monitor.total, monitor.tracking[0]["consecutive"]))
        return True
    return False


def test_fires_on_third_reading():
    """Two failing readings after grace are not enough; the third confirms, with its minutes."""
    monitor = charging_monitor()
    feed(monitor, [60, 120, 180, 240], power=0)
    if feed(monitor, [GRACE_S, GRACE_S + 60], power=0):
        print("ERROR: fired before the third failing reading")
        return True
    events = feed(monitor, [GRACE_S + 120], power=0)
    if len(events) != 1 or monitor.total != 1:
        print("ERROR: expected exactly one event on the third reading, got {} (total {})".format(events, monitor.total))
        return True
    event = events[0]
    expected = {"inverter": 0, "mode": MODE_CHARGING, "slot_start": SLOT_START, "slot_end": SLOT_END, "at": T0 + GRACE_S + 120, "requested_w": 3000, "measured_w": 0}
    for key, value in expected.items():
        if event.get(key) != value:
            print("ERROR: event {} is {}, expected {}".format(key, event.get(key), value))
            return True
    # Reading at 300 credits nothing (grace ended at 300), then 60s + 60s
    if not close(event["failed_minutes"], 2.0):
        print("ERROR: failed_minutes {} expected 2.0".format(event["failed_minutes"]))
        return True
    return False


def test_passing_reading_resets():
    """A passing reading resets the run, and the minutes before it are not carried over."""
    monitor = charging_monitor()
    feed(monitor, [240], power=0)
    feed(monitor, [GRACE_S, GRACE_S + 60], power=0)
    feed(monitor, [GRACE_S + 120], power=-3000)
    if feed(monitor, [GRACE_S + 180, GRACE_S + 240], power=0):
        print("ERROR: the run was not reset by a passing reading")
        return True
    events = feed(monitor, [GRACE_S + 300], power=0)
    if len(events) != 1 or not close(events[0]["failed_minutes"], 3.0):
        print("ERROR: expected one event with 3.0 minutes after the reset, got {}".format(events))
        return True
    return False


def test_fires_once_and_minutes_grow():
    """Later failing readings in the same slot add minutes to the event but never re-fire."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    more = feed(monitor, [GRACE_S + 180, GRACE_S + 240, GRACE_S + 300], power=0)
    if more or monitor.total != 1 or len(monitor.events) != 1:
        print("ERROR: slot fired more than once: {} total {}".format(more, monitor.total))
        return True
    if not close(monitor.events[0]["failed_minutes"], 5.0):
        print("ERROR: failed_minutes {} expected 5.0".format(monitor.events[0]["failed_minutes"]))
        return True
    return False


def test_new_slot_resets_grace():
    """A non-contiguous new slot starts its own grace."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    later = SLOT_END + 600
    monitor.set_expectation(0, MODE_CHARGING, later, later + 3600, 80.0, T0 + 4500)
    track = monitor.tracking[0]
    if track["entered_at"] != T0 + 4500 or track["event"] is not None or track["slot_start"] != later:
        print("ERROR: new slot did not start fresh tracking: {}".format(track))
        return True
    if feed(monitor, [4560, 4620, 4680, 4740], power=0):
        print("ERROR: new slot judged inside its own grace")
        return True
    return False


def test_contiguous_window_is_same_slot():
    """The next back-to-back window continues the slot: same key, same grace, no second count."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START + 1800, SLOT_END + 1800, 80.0, T0 + 1800)
    track = monitor.tracking[0]
    if track["slot_start"] != SLOT_START or track["entered_at"] != T0 or track["event"] is None:
        print("ERROR: contiguous window started a new slot: {}".format(track))
        return True
    if feed(monitor, [1860, 1920, 1980], power=0) or monitor.total != 1:
        print("ERROR: contiguous window counted again, total {}".format(monitor.total))
        return True
    if monitor.events[0]["slot_end"] != SLOT_END + 1800:
        print("ERROR: event slot_end not extended: {}".format(monitor.events[0]["slot_end"]))
        return True
    return False


def test_new_mode_resets_grace():
    """A different mode in the same slot is a new slot with its own grace."""
    monitor = charging_monitor()
    monitor.set_expectation(0, MODE_FREEZE_CHARGING, SLOT_START, SLOT_END, None, T0 + 900)
    track = monitor.tracking[0]
    if track["mode"] != MODE_FREEZE_CHARGING or track["entered_at"] != T0 + 900:
        print("ERROR: new mode did not start fresh tracking: {}".format(track))
        return True
    return False


def test_reentry_does_not_recount():
    """Charging -> Hold charging -> Charging in the same slot re-attaches to the existing event."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    monitor.clear_expectation(0)
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START, SLOT_END, 80.0, T0 + 900)
    if monitor.tracking[0]["event"] is not monitor.events[0]:
        print("ERROR: re-entry did not re-attach to the slot's event")
        return True
    before = monitor.events[0]["failed_minutes"]
    if feed(monitor, [1200, 1260, 1320], power=0) or monitor.total != 1:
        print("ERROR: re-entry counted the slot again, total {}".format(monitor.total))
        return True
    if not monitor.events[0]["failed_minutes"] > before:
        print("ERROR: re-entered slot did not accrue minutes")
        return True
    return False


def test_target_reached_is_silent():
    """Charging at/above target and Exporting at/below target are never judged."""
    monitor = charging_monitor(target=80.0)
    if feed(monitor, [240, 300, 360, 420, 480], power=0, soc=80.0) or monitor.tracking[0]["consecutive"] != 0:
        print("ERROR: Charging at target was judged")
        return True
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_EXPORTING, SLOT_START, SLOT_END, 20.0, T0)
    if feed(monitor, [240, 300, 360, 420, 480], power=0, soc=20.0):
        print("ERROR: Exporting at target was judged")
        return True
    return False


def test_low_requested_rate_is_silent():
    """A requested rate below the floor (low power, balancer hold) is never judged."""
    monitor = charging_monitor()
    if feed(monitor, [240, 300, 360, 420, 480], power=0, charge_w=FLOOR_W - 1):
        print("ERROR: a below-floor requested rate was judged")
        return True
    if feed(monitor, [540, 600, 660, 720], power=0, charge_w=None):
        print("ERROR: an unwritten (None) requested rate was judged")
        return True
    return False


def test_exporting_fails_on_low_discharge():
    """Exporting fails below 25% of the requested discharge rate and records that rate."""
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_EXPORTING, SLOT_START, SLOT_END, 20.0, T0)
    if feed(monitor, [240, 300, 360, 420], power=600, discharge_w=2000):
        print("ERROR: Exporting at 30% of the requested rate was judged failing")
        return True
    events = feed(monitor, [480, 540, 600], power=400, discharge_w=2000)
    if len(events) != 1 or events[0]["requested_w"] != 2000:
        print("ERROR: Exporting at 20% did not fail once with requested_w 2000: {}".format(events))
        return True
    return False


def test_freeze_modes():
    """Freeze charging fails on discharge, Freeze exporting on charge, each only past the floor."""
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_FREEZE_CHARGING, SLOT_START, SLOT_END, None, T0)
    if feed(monitor, [240, 300, 360, 420], power=FLOOR_W):
        print("ERROR: Freeze charging at the floor was judged failing")
        return True
    events = feed(monitor, [480, 540, 600], power=500)
    if len(events) != 1 or events[0]["requested_w"] is not None:
        print("ERROR: Freeze charging discharging at 500W did not fail once: {}".format(events))
        return True

    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_FREEZE_EXPORTING, SLOT_START, SLOT_END, None, T0)
    if feed(monitor, [240, 300, 360, 420], power=3000):
        print("ERROR: Freeze exporting while discharging was judged failing")
        return True
    if len(feed(monitor, [480, 540, 600], power=-500)) != 1:
        print("ERROR: Freeze exporting charging at 500W did not fail once")
        return True
    return False


def test_gap_is_capped():
    """A long gap between readings credits at most MAX_GAP_S."""
    monitor = charging_monitor()
    feed(monitor, [240], power=0)
    events = feed(monitor, [GRACE_S, GRACE_S + 60, GRACE_S + 400], power=0)
    # 0 + 60 + min(340, 120) seconds = 3.0 minutes
    if len(events) != 1 or not close(events[0]["failed_minutes"], 3.0):
        print("ERROR: expected 3.0 capped minutes, got {}".format(events))
        return True
    return False


def test_close_readings_are_ignored():
    """Readings closer than MIN_GAP_S to the previous one are ignored entirely."""
    monitor = charging_monitor()
    feed(monitor, [240], power=0)
    feed(monitor, [GRACE_S, GRACE_S + 10, GRACE_S + 20], power=0)
    if monitor.tracking[0]["consecutive"] != 1:
        print("ERROR: close readings counted, consecutive {}".format(monitor.tracking[0]["consecutive"]))
        return True
    return False


def test_clear_keeps_events():
    """Clearing stops observation but keeps recorded events; clearing None clears every inverter."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    monitor.set_expectation(1, MODE_CHARGING, SLOT_START, SLOT_END, 80.0, T0)
    monitor.clear_expectation(0)
    if 0 in monitor.tracking or len(monitor.events) != 1:
        print("ERROR: clear_expectation(0) lost the event or kept tracking")
        return True
    if monitor.observe(0, 0, 50.0, 3000, 3000, T0 + 900) is not None:
        print("ERROR: a cleared inverter was observed")
        return True
    monitor.clear_expectation()
    if monitor.tracking:
        print("ERROR: clear_expectation() left tracking {}".format(monitor.tracking))
        return True
    return False


def make_event(at, mode=MODE_CHARGING, inverter_id=0, slot_start=SLOT_START, failed_minutes=2.0):
    """A well-formed event as it would be read back from the sensor attribute."""
    return {"inverter": inverter_id, "mode": mode, "slot_start": slot_start, "slot_end": slot_start + 3600, "at": at, "requested_w": 3000, "measured_w": 0, "failed_minutes": failed_minutes}


def test_recent_and_prune():
    """recent_events() and prune() use the 24h window; prune keeps an event a live slot still owns."""
    monitor = ControlMonitor()
    monitor.events = [make_event(T0, slot_start=T0), make_event(T0 + DEFAULT_WINDOW_S + 100, slot_start=T0 + DEFAULT_WINDOW_S)]
    now = T0 + DEFAULT_WINDOW_S + 200
    if [event["at"] for event in monitor.recent_events(now)] != [T0 + DEFAULT_WINDOW_S + 100]:
        print("ERROR: recent_events returned {}".format(monitor.recent_events(now)))
        return True
    monitor.tracking[0] = {"event": monitor.events[0]}
    monitor.prune(now)
    if len(monitor.events) != 2:
        print("ERROR: prune dropped an event still attached to a live slot")
        return True
    monitor.tracking.clear()
    monitor.prune(now)
    if len(monitor.events) != 1:
        print("ERROR: prune kept an expired event: {}".format(monitor.events))
        return True
    return False


def test_newest_events_and_by_mode():
    """newest_events keeps the newest N oldest-first; by_mode counts per mode."""
    monitor = ControlMonitor()
    monitor.events = [make_event(T0 + i, slot_start=T0 + i) for i in range(25)]
    newest = monitor.newest_events(20)
    if len(newest) != 20 or newest[0]["at"] != T0 + 5 or newest[-1]["at"] != T0 + 24:
        print("ERROR: newest_events(20) returned the wrong slice")
        return True
    counts = ControlMonitor.by_mode([make_event(T0), make_event(T0 + 1, mode=MODE_EXPORTING), make_event(T0 + 2)])
    if counts != {MODE_CHARGING: 2, MODE_EXPORTING: 1}:
        print("ERROR: by_mode returned {}".format(counts))
        return True
    return False


def test_restore_merges_and_dedupes():
    """Restore merges valid events by key (keeping the larger minutes), skips junk, sorts by time."""
    monitor = ControlMonitor()
    monitor.events = [make_event(T0 + 50, slot_start=T0, failed_minutes=2.0)]
    restored = [
        make_event(T0 + 50, slot_start=T0, failed_minutes=5.0),
        make_event(T0 + 10, slot_start=T0 + 7200),
        {"inverter": 0, "mode": "Demand", "slot_start": T0, "slot_end": T0, "at": T0, "failed_minutes": 0},
        {"inverter": True, "mode": MODE_CHARGING, "slot_start": T0, "slot_end": T0, "at": T0, "failed_minutes": 0},
        "not an event",
    ]
    if not monitor.restore(restored, "7"):
        print("ERROR: restore with a numeric total did not resolve")
        return True
    if len(monitor.events) != 2 or monitor.events[0]["at"] != T0 + 10:
        print("ERROR: merged events wrong or unsorted: {}".format(monitor.events))
        return True
    if not close(monitor.events[1]["failed_minutes"], 5.0):
        print("ERROR: duplicate did not keep the larger failed_minutes")
        return True
    if monitor.total != 7 or not monitor.restored:
        print("ERROR: total {} restored {}".format(monitor.total, monitor.restored))
        return True
    return False


def test_restore_total_adds_once():
    """The total merges as restored + counted, once; unreadable totals leave it unresolved."""
    monitor = ControlMonitor()
    for unreadable in (None, "unavailable", "nan", "inf", "-3"):
        if monitor.restore([], unreadable) or monitor.restored:
            print("ERROR: restore resolved on unreadable total {!r}".format(unreadable))
            return True
    monitor.total = 2  # failures counted by this process before the restore resolved
    if not monitor.restore([], "5") or monitor.total != 7:
        print("ERROR: expected restored + counted = 7, got {}".format(monitor.total))
        return True
    monitor.restore([], "5")
    if monitor.total != 7:
        print("ERROR: a second restore added the total again: {}".format(monitor.total))
        return True
    return False


def failed_slot_store():
    """The events a monitor that saw the slot T0..T0+1h fail would leave in the sensor attribute."""
    first = charging_monitor()
    feed(first, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    return json.loads(json.dumps(first.newest_events(20)))  # what HA hands back


def test_restart_mid_slot_does_not_recount():
    """After a restart mid-slot (plan now on the next contiguous window) the slot is not re-counted - restore first."""
    stored = failed_slot_store()
    second = ControlMonitor()
    second.restore(stored, "1")
    second.set_expectation(0, MODE_CHARGING, SLOT_START + 1800, SLOT_END + 1800, 80.0, T0 + 2000)
    if feed(second, [2300, 2360, 2420, 2480], power=0) or second.total != 1:
        print("ERROR: restart re-counted the slot, total {}".format(second.total))
        return True
    if not second.events[0]["failed_minutes"] > stored[0]["failed_minutes"]:
        print("ERROR: restored event did not keep accruing minutes")
        return True
    return False


def test_restart_mid_slot_production_order():
    """
    The production order after a restart: execute_plan sets the expectation BEFORE the publish
    cycle restores the events, and later plan runs extend that tracking in place. The restored
    event must still own the slot, so it is neither re-counted nor duplicated.
    """
    stored = failed_slot_store()
    second = ControlMonitor()
    second.set_expectation(0, MODE_CHARGING, SLOT_START + 1800, SLOT_END + 1800, 80.0, T0 + 2000)
    second.restore(stored, "1")
    second.set_expectation(0, MODE_CHARGING, SLOT_START + 2100, SLOT_END + 2100, 80.0, T0 + 2100)
    events = feed(second, [2300, 2360, 2420, 2480, 2540], power=0)
    if events or second.total != 1 or len(second.events) != 1:
        print("ERROR: production-order restart re-counted the slot: total {} events {}".format(second.total, second.events))
        return True
    if not second.events[0]["failed_minutes"] > stored[0]["failed_minutes"]:
        print("ERROR: restored event did not keep accruing minutes")
        return True
    if second.tracking[0]["slot_start"] != SLOT_START or second.events[0]["slot_end"] != SLOT_END + 2100:
        print("ERROR: attached tracking/event slot not merged: tracking {} event {}".format(second.tracking[0], second.events[0]))
        return True
    return False


def test_extend_attaches_existing_event():
    """
    An event for the slot that appears while unattached tracking already exists is picked up by
    the next contiguous extension. The event is put in directly, bypassing restore()'s own
    attach, so the extend path is proven on its own.
    """
    stored = failed_slot_store()
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START + 1800, SLOT_END + 1800, 80.0, T0 + 2000)
    monitor.events.append(dict(stored[0]))
    monitor.total = 1
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START + 2100, SLOT_END + 2100, 80.0, T0 + 2100)
    if monitor.tracking[0]["event"] is not monitor.events[0]:
        print("ERROR: contiguous extension did not attach the existing event: {}".format(monitor.tracking[0]))
        return True
    if feed(monitor, [2300, 2360, 2420, 2480], power=0) or monitor.total != 1 or len(monitor.events) != 1:
        print("ERROR: extension re-counted the slot: total {} events {}".format(monitor.total, monitor.events))
        return True
    if monitor.tracking[0]["slot_start"] != SLOT_START or monitor.events[0]["slot_end"] != SLOT_END + 2100:
        print("ERROR: attached slot not merged: tracking {} event {}".format(monitor.tracking[0], monitor.events[0]))
        return True
    return False


def test_nothing_after_slot_end():
    """
    Readings at or after slot_end are not judged: the slot is over, and a stale expectation (a
    skipped plan run) must neither fire nor credit minutes to an existing event.
    """
    monitor = charging_monitor()
    if feed(monitor, [SLOT_END - SLOT_START + offset for offset in (0, 60, 120, 180, 240)], power=0) or monitor.total != 0:
        print("ERROR: readings after slot_end fired: total {}".format(monitor.total))
        return True
    monitor = charging_monitor()
    if len(feed(monitor, [GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)) != 1:
        print("ERROR: setup - the slot did not fail inside its window")
        return True
    minutes = monitor.events[0]["failed_minutes"]
    feed(monitor, [SLOT_END - SLOT_START + offset for offset in (0, 60, 120)], power=0)
    if not close(monitor.events[0]["failed_minutes"], minutes) or monitor.total != 1:
        print("ERROR: readings after slot_end credited minutes: {} -> {}".format(minutes, monitor.events[0]["failed_minutes"]))
        return True
    return False


def test_shortened_slot_stops_judgement():
    """
    A replan that shortens the active window moves the observation deadline in, even though the
    slot's span (kept for de-duplicating events) still reaches the old end. Otherwise a skipped plan
    run leaves the inverter, correctly back in Demand, judged against the old window.
    """
    monitor = charging_monitor()
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START, SLOT_START + 600, 80.0, T0 + 60)
    if monitor.tracking[0]["slot_end"] != SLOT_END:
        print("ERROR: the slot span should still reach the old end, got {}".format(monitor.tracking[0]["slot_end"]))
        return True
    if feed(monitor, [660, 720, 780, 840], power=0) or monitor.total != 0:
        print("ERROR: readings after the shortened end were judged: total {}".format(monitor.total))
        return True

    # Attaching to an existing (restored) event must not widen the deadline back to the event's end
    stored = [make_event(T0 + 400, slot_start=SLOT_START)]
    monitor = ControlMonitor()
    monitor.restore(stored, "1")
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START + 300, SLOT_START + 900, 80.0, T0 + 300)
    if monitor.tracking[0]["event"] is None or monitor.tracking[0]["deadline"] != SLOT_START + 900:
        print("ERROR: attach changed the deadline: {}".format(monitor.tracking[0]))
        return True
    before = monitor.events[0]["failed_minutes"]
    feed(monitor, [960, 1020, 1080], power=0)
    if not close(monitor.events[0]["failed_minutes"], before):
        print("ERROR: readings past the commanded end credited minutes to the restored event")
        return True
    return False


def test_format_failure():
    """The Warn: line names the inverter, mode, slot and the power shortfall."""
    failed = False
    cases = [
        (make_event(T0), "Warn: Control failure: inverter 0 Charging 00:00-01:00 - battery idle vs 3000W charge requested"),
        (dict(make_event(T0), measured_w=-120), "Warn: Control failure: inverter 0 Charging 00:00-01:00 - battery charging at 120W vs 3000W charge requested"),
        (dict(make_event(T0, mode=MODE_EXPORTING), requested_w=2000, measured_w=300), "Warn: Control failure: inverter 0 Exporting 00:00-01:00 - battery discharging at 300W vs 2000W discharge requested"),
        (dict(make_event(T0, mode=MODE_FREEZE_CHARGING), requested_w=None, measured_w=450), "Warn: Control failure: inverter 0 Freeze charging 00:00-01:00 - battery discharging at 450W, Freeze charging allows at most 200W discharge"),
        (dict(make_event(T0, mode=MODE_FREEZE_EXPORTING, inverter_id=1), requested_w=None, measured_w=-450), "Warn: Control failure: inverter 1 Freeze exporting 00:00-01:00 - battery charging at 450W, Freeze exporting allows at most 200W charge"),
    ]
    for event, expected in cases:
        got = format_failure(event, pytz.utc)
        if got != expected:
            print("ERROR: format_failure gave\n  {}\nexpected\n  {}".format(got, expected))
            failed = True
    return failed


class RestoreSource:
    """
    Stands in for load_previous_value_from_ha (the full state + history read) and get_state_wrapper
    (the state-only read) so a test decides exactly what the restore reads, and counts each kind.
    """

    def __init__(self, my_predbat, events=None, total=None):
        """Wrap my_predbat's loaders, answering for the two control-failure entities only."""
        self.my_predbat = my_predbat
        self.events = events
        self.total = total
        self.full_reads = 0
        self.state_reads = 0
        self.original = my_predbat.load_previous_value_from_ha
        self.original_state = my_predbat.get_state_wrapper
        my_predbat.load_previous_value_from_ha = self.load
        my_predbat.get_state_wrapper = self.state

    def answer(self, entity, attribute):
        """The configured value for a control-failure entity, or the string NOT_OURS for anything else."""
        if entity == self.my_predbat.prefix + ".control_failures" and attribute == "events":
            return self.events
        if entity == self.my_predbat.prefix + ".control_failures_total" and attribute is None:
            return self.total
        return "NOT_OURS"

    def load(self, entity, attribute=None):
        """Full read: return the configured value for the control-failure entities, defer for anything else."""
        value = self.answer(entity, attribute)
        if value == "NOT_OURS":
            return self.original(entity, attribute=attribute)
        self.full_reads += 1
        return value

    def state(self, entity_id=None, default=None, attribute=None, **kwargs):
        """State-only read: as load(), counted separately; unreadable reads as 'unavailable' as HA would."""
        value = self.answer(entity_id, attribute)
        if value == "NOT_OURS":
            return self.original_state(entity_id=entity_id, default=default, attribute=attribute, **kwargs)
        self.state_reads += 1
        return "unavailable" if value is None else value

    def close(self):
        """Put the real loaders back."""
        self.my_predbat.load_previous_value_from_ha = self.original
        self.my_predbat.get_state_wrapper = self.original_state


def publish_setup(my_predbat):
    """Give my_predbat a fresh monitor and forget any earlier publish of the two entities."""
    saved = my_predbat.control_monitor
    my_predbat.control_monitor = ControlMonitor()
    for suffix in (".control_failures", ".control_failures_total"):
        my_predbat.dashboard_values.pop(my_predbat.prefix + suffix, None)
    return saved


def published(my_predbat, suffix):
    """The state last published for an entity, or None if it was never written."""
    item = my_predbat.dashboard_values.get(my_predbat.prefix + suffix)
    return None if item is None else item["state"]


def test_publish_fresh_install(my_predbat):
    """Nothing to restore and nothing counted: the 24h sensor publishes 0, the total is not written."""
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat)
    failed = False
    try:
        my_predbat.publish_control_failures(T0)
        if published(my_predbat, ".control_failures") != 0:
            print("ERROR: 24h sensor not published as 0")
            failed = True
        if published(my_predbat, ".control_failures_total") is not None or my_predbat.control_monitor.restored:
            print("ERROR: a 0 total was written over a total that could not be read")
            failed = True
        # The first counted failure is written, and resolves the restore
        my_predbat.control_monitor.events.append(make_event(T0 + 60))
        my_predbat.control_monitor.total = 1
        my_predbat.publish_control_failures(T0 + 120)
        if published(my_predbat, ".control_failures_total") != 1 or not my_predbat.control_monitor.restored:
            print("ERROR: first failure on a fresh install did not publish the total")
            failed = True
        attributes = my_predbat.dashboard_values[my_predbat.prefix + ".control_failures"]["attributes"]
        if published(my_predbat, ".control_failures") != 1 or attributes["by_mode"] != {MODE_CHARGING: 1} or len(attributes["events"]) != 1:
            print("ERROR: 24h sensor attributes wrong: {}".format(attributes))
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed


def test_publish_restores_total_and_events(my_predbat):
    """A readable total and events are restored and re-published."""
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat, events=[make_event(T0 - 60)], total="7")
    failed = False
    try:
        my_predbat.publish_control_failures(T0)
        if published(my_predbat, ".control_failures_total") != 7 or published(my_predbat, ".control_failures") != 1:
            print("ERROR: restore not published: total {} 24h {}".format(published(my_predbat, ".control_failures_total"), published(my_predbat, ".control_failures")))
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed


def test_publish_late_restore_adds_counted(my_predbat):
    """Unreadable at first, readable later, with a failure counted in between: total = restored + counted."""
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat)
    failed = False
    try:
        my_predbat.publish_control_failures(T0)
        my_predbat.control_monitor.total = 1  # counted between the two publish cycles
        source.total = "5"
        my_predbat.publish_control_failures(T0 + 300)
        if published(my_predbat, ".control_failures_total") != 6:
            print("ERROR: late restore published {} expected 6".format(published(my_predbat, ".control_failures_total")))
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed


def test_publish_history_read_cadence(my_predbat):
    """
    While the total is unresolved the full (state + 30-day history) read runs on the first attempt
    and every 12th after it; the cycles in between read state only - and a state-only read that
    finds the total still resolves the restore.
    """
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat)
    failed = False
    try:
        for cycle in range(25):
            my_predbat.publish_control_failures(T0 + cycle * 300)
        # Full reads on attempts 1, 13 and 25; two entities each
        if source.full_reads != 6 or source.state_reads != 44:
            print("ERROR: expected 6 full and 44 state-only reads, got {} full {} state".format(source.full_reads, source.state_reads))
            failed = True
        if my_predbat.control_monitor.restored:
            print("ERROR: restore resolved with nothing to read")
            failed = True
        source.total = "4"
        source.events = [make_event(T0 + 7000)]
        my_predbat.publish_control_failures(T0 + 7500)  # attempt 26: state-only
        if source.full_reads != 6 or not my_predbat.control_monitor.restored or published(my_predbat, ".control_failures_total") != 4:
            print("ERROR: state-only read did not resolve the restore: full {} restored {} total {}".format(source.full_reads, my_predbat.control_monitor.restored, published(my_predbat, ".control_failures_total")))
            failed = True
        if len(my_predbat.control_monitor.events) != 1:
            print("ERROR: state-only read did not restore the events: {}".format(my_predbat.control_monitor.events))
            failed = True
        reads = source.full_reads + source.state_reads
        my_predbat.publish_control_failures(T0 + 7800)
        if source.full_reads + source.state_reads != reads:
            print("ERROR: a resolved restore kept reading")
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed


def run_control_monitor_tests(my_predbat):
    """Run all control failure monitor tests."""
    failed = False
    for name, fn in [
        ("judge_reading", test_judge_reading),
        ("nothing_during_grace", test_nothing_during_grace),
        ("fires_on_third_reading", test_fires_on_third_reading),
        ("passing_reading_resets", test_passing_reading_resets),
        ("fires_once_minutes_grow", test_fires_once_and_minutes_grow),
        ("new_slot_resets_grace", test_new_slot_resets_grace),
        ("contiguous_window_same_slot", test_contiguous_window_is_same_slot),
        ("new_mode_resets_grace", test_new_mode_resets_grace),
        ("reentry_does_not_recount", test_reentry_does_not_recount),
        ("target_reached_silent", test_target_reached_is_silent),
        ("low_requested_rate_silent", test_low_requested_rate_is_silent),
        ("exporting_low_discharge", test_exporting_fails_on_low_discharge),
        ("freeze_modes", test_freeze_modes),
        ("gap_is_capped", test_gap_is_capped),
        ("close_readings_ignored", test_close_readings_are_ignored),
        ("clear_keeps_events", test_clear_keeps_events),
        ("recent_and_prune", test_recent_and_prune),
        ("newest_and_by_mode", test_newest_events_and_by_mode),
        ("restore_merges_dedupes", test_restore_merges_and_dedupes),
        ("restore_total_adds_once", test_restore_total_adds_once),
        ("restart_mid_slot", test_restart_mid_slot_does_not_recount),
        ("restart_mid_slot_production_order", test_restart_mid_slot_production_order),
        ("extend_attaches_existing_event", test_extend_attaches_existing_event),
        ("nothing_after_slot_end", test_nothing_after_slot_end),
        ("shortened_slot_stops_judgement", test_shortened_slot_stops_judgement),
        ("format_failure", test_format_failure),
    ]:
        print("**** control_monitor: {} ****".format(name))
        if fn():
            print("ERROR: control_monitor test {} failed".format(name))
            failed = True
    for name, fn in [
        ("publish_fresh_install", test_publish_fresh_install),
        ("publish_restores", test_publish_restores_total_and_events),
        ("publish_late_restore", test_publish_late_restore_adds_counted),
        ("publish_history_read_cadence", test_publish_history_read_cadence),
    ]:
        print("**** control_monitor: {} ****".format(name))
        if fn(my_predbat):
            print("ERROR: control_monitor test {} failed".format(name))
            failed = True
    return failed
