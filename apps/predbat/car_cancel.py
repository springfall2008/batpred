# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""
Shared rules for cancelling a car charging slot so the battery can export over it.

Which car slots an export may cancel, and which of those a given set of export windows does cancel.
Both the plan search and the prediction engines use these, so they cannot disagree.
"""

from typing import NamedTuple

from const import EXPORT_MODE_TARGET
from utils import dp2, export_mode_of

# The most slots the planner tracks as cancellable: the cancelled set is a bit mask
CAR_CANCEL_MAX_SLOTS = 64


class CancelSlot(NamedTuple):
    """A car charging slot that an export may cancel."""

    car_n: int
    index: int  # Position in that car's car_charging_slots_model() list
    start: int
    end: int
    forced: bool  # A charger component is force-holding the car off, so the slot is cancelled whatever the plan does


def cancellable_slots(car_slots_model, now_slot_counts, cancellable_cars, held_until, minutes_now):
    """
    The car slots an export may cancel, sorted by (start, car_n) and capped at CAR_CANCEL_MAX_SLOTS.

    A slot qualifies when its car is cancellable or force-held, it is not a car_charging_now_slots
    entry (those come first in the model list, now_slot_counts of them), it has energy and it ends
    after now. held_until maps car_n to an absolute minute; a slot starting before it is forced.
    """
    slots = []
    for car_n, car_slots in enumerate(car_slots_model):
        if car_n not in cancellable_cars and car_n not in held_until:
            continue
        first = now_slot_counts[car_n] if car_n < len(now_slot_counts) else 0
        hold_end = held_until.get(car_n)
        for index, slot in enumerate(car_slots):
            if index < first or dp2(slot.get("kwh", 0)) <= 0 or slot["end"] <= minutes_now:
                continue
            forced = hold_end is not None and slot["start"] < hold_end
            slots.append(CancelSlot(car_n, index, slot["start"], slot["end"], forced))
    slots.sort(key=lambda slot: (slot.start, slot.car_n))
    return slots[:CAR_CANCEL_MAX_SLOTS]


def cancelled_mask(slots, export_window, export_limits):
    """
    Bit j set for every slot j that is forced, or that a target-mode export window overlaps.

    Freeze and idle windows export nothing, so they cancel nothing.
    """
    mask = 0
    for j, slot in enumerate(slots):
        if slot.forced:
            mask |= 1 << j
            continue
        for window, limit in zip(export_window, export_limits):
            if export_mode_of(limit) == EXPORT_MODE_TARGET and window["start"] < slot.end and window["end"] > slot.start:
                mask |= 1 << j
                break
    return mask


def cancelled_windows(slots, mask):
    """car_n -> the (start, end) of every slot in the mask, time-ordered."""
    result = {}
    for j, slot in enumerate(slots):
        if mask & (1 << j):
            result.setdefault(slot.car_n, []).append((slot.start, slot.end))
    for windows in result.values():
        windows.sort()
    return result
