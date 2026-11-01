# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""Tests for the shared rules on which car slots an export may cancel."""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from car_cancel import CAR_CANCEL_MAX_SLOTS, CancelSlot, cancellable_slots, cancelled_mask, cancelled_windows
from const import EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET
from utils import pack_export_limit
from tests.test_infra import run_async
from tests.test_car_charger_control import FakeCharger, FakeComponent


def _slot(start, end, kwh=1.0):
    """A car charging slot."""
    return {"start": start, "end": end, "kwh": kwh, "octopus": False}


def test_cancellable_slots_rules():
    """Only Predbat-led and holdable cars qualify, and not empty, past or charging-now slots."""
    model = [
        [_slot(0, 30), _slot(60, 90), _slot(120, 150, 0), _slot(150, 180)],  # car 0: index 0 is a charging-now slot
        [_slot(60, 90)],  # car 1 is neither
        [_slot(60, 90), _slot(30, 60)],  # car 2 is holdable
    ]
    slots = cancellable_slots(model, [1, 0, 0], {0, 2}, {}, 60)
    got = [(s.car_n, s.index, s.start, s.end) for s in slots]
    # car 0 index 0 (now slot), index 2 (0 kWh); car 2 index 1 ends at minutes_now
    assert got == [(0, 1, 60, 90), (2, 0, 60, 90), (0, 3, 150, 180)], got
    assert not any(s.forced for s in slots)


def test_cancellable_slots_cap():
    """The list is capped, keeping the earliest slots."""
    model = [[_slot(i * 30, i * 30 + 30) for i in range(70)]]
    slots = cancellable_slots(model, [0], {0}, {}, 0)
    assert len(slots) == CAR_CANCEL_MAX_SLOTS
    assert slots[-1].start == 63 * 30
    assert all(s.start != 64 * 30 for s in slots)


def test_cancellable_slots_forced():
    """Slots starting before the hold ends are forced, and a held car need not be cancellable."""
    model = [[_slot(30, 60), _slot(60, 90), _slot(90, 120)]]
    slots = cancellable_slots(model, [0], {0}, {0: 90}, 0)
    assert [s.forced for s in slots] == [True, True, False], slots
    slots = cancellable_slots(model, [0], set(), {0: 61}, 0)
    assert [s.forced for s in slots] == [True, True, False], slots
    assert len(slots) == 3
    assert cancellable_slots(model, [0], set(), {}, 0) == []


def test_cancelled_mask():
    """Target windows cancel what they overlap; freeze, idle and non-overlapping windows do not."""
    slots = [CancelSlot(0, 0, 60, 90, False), CancelSlot(0, 1, 120, 150, True), CancelSlot(0, 2, 180, 210, False)]
    target = pack_export_limit(EXPORT_MODE_TARGET, 10)
    # One minute of overlap with slot 0
    assert cancelled_mask(slots, [{"start": 30, "end": 61}], [target]) == 0b010 | 0b001
    # Ends exactly at slot start: nothing but the forced slot
    assert cancelled_mask(slots, [{"start": 30, "end": 60}], [target]) == 0b010
    # Starts exactly at slot end
    assert cancelled_mask(slots, [{"start": 90, "end": 120}], [target]) == 0b010
    for mode in (EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE):
        limit = pack_export_limit(mode)
        assert cancelled_mask(slots, [{"start": 60, "end": 90}, {"start": 180, "end": 210}], [limit, limit]) == 0b010
    assert cancelled_mask(slots, [], []) == 0b010


def test_cancelled_windows():
    """A mask groups the chosen slots by car, time-ordered."""
    slots = [CancelSlot(1, 0, 60, 90, False), CancelSlot(0, 0, 90, 120, False), CancelSlot(1, 1, 120, 150, False)]
    assert cancelled_windows(slots, 0b101) == {1: [(60, 90), (120, 150)]}
    assert cancelled_windows(slots, 0b110) == {0: [(90, 120)], 1: [(120, 150)]}
    assert cancelled_windows(slots, 0) == {}


def _set_switch(my_predbat, value):
    """Set the cancel switch the way the UI does."""
    my_predbat.expose_config("car_charging_cancel_for_export", value, force=True)


def test_car_cancellable_cars(my_predbat):
    """Only with the switch on, and then Predbat-led plus holdable cars."""
    names = ("num_cars", "car_planned_by_predbat", "car_force_holdable")
    saved = {name: getattr(my_predbat, name) for name in names}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        my_predbat.num_cars = 2
        my_predbat.car_planned_by_predbat = {0}
        my_predbat.car_force_holdable = {1: "Fake"}
        _set_switch(my_predbat, False)
        assert my_predbat.car_cancellable_cars() == set()
        assert my_predbat.car_cancel_slots() == []
        _set_switch(my_predbat, True)
        assert my_predbat.car_cancellable_cars() == {0, 1}
        my_predbat.num_cars = 0
        assert not my_predbat.car_cancel_enabled()
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_car_cancel_slots(my_predbat):
    """The PredBat wrapper puts now slots ahead, and converts the hold end to a minute."""
    names = ("num_cars", "car_charging_slots", "car_charging_now_slots", "car_planned_by_predbat", "car_force_holdable", "car_force_hold_until", "minutes_now")
    saved = {name: getattr(my_predbat, name) for name in names}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        _set_switch(my_predbat, True)
        my_predbat.num_cars = 1
        my_predbat.minutes_now = 60
        my_predbat.car_charging_now_slots = [[_slot(60, 90)]]
        my_predbat.car_charging_slots = [[_slot(60, 90), _slot(120, 150), _slot(180, 210)]]
        my_predbat.car_planned_by_predbat = {0}
        my_predbat.car_force_holdable = {}
        # Held until 02:30:30 after midnight: ceil gives minute 151 so the slots at 60 and 120 are forced
        my_predbat.car_force_hold_until = {0: my_predbat.midnight_utc + timedelta(minutes=150, seconds=30)}
        slots = my_predbat.car_cancel_slots()
        assert [(s.index, s.start, s.forced) for s in slots] == [(1, 60, True), (2, 120, True), (3, 180, False)], slots
        # A hold that has ended is dropped
        my_predbat.car_force_hold_until = {0: my_predbat.midnight_utc + timedelta(minutes=60)}
        assert not any(s.forced for s in my_predbat.car_cancel_slots())
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_force_hold_tick_publishes_holdable():
    """The component publishes the cars it can hold, before its early return, and removes only its own."""
    component = FakeComponent([FakeCharger("a")])
    name = component.charger_control_log_name
    base = component.base
    base.car_force_holdable = {}
    now = None
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {0: name}, base.car_force_holdable
    component.args["car_charging_cancel_for_export"] = False
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {}, base.car_force_holdable
    # Another component's entry is left alone
    base.car_force_holdable = {0: "Other"}
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {0: "Other"}
    component.args["car_charging_cancel_for_export"] = True
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {0: name}, "the switch on takes the car over"
    base.car_force_holdable = {0: name}
    component.chargers[0].can_force = False
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {}


def run_car_cancel_tests(my_predbat):
    """Run the car cancel tests. Returns True on failure."""
    print("**** Running car cancel tests ****")
    test_cancellable_slots_rules()
    test_cancellable_slots_cap()
    test_cancellable_slots_forced()
    test_cancelled_mask()
    test_cancelled_windows()
    test_car_cancellable_cars(my_predbat)
    test_car_cancel_slots(my_predbat)
    test_force_hold_tick_publishes_holdable()
    print("**** car cancel tests passed ****")
    return False
