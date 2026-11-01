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

The cancel table works out, once per plan, everything a prediction needs per step to cancel a slot,
so the engines' hot loops only look things up.
"""

from typing import NamedTuple

from const import EXPORT_MODE_TARGET, PREDICT_STEP
from utils import dp2, export_mode_of, round_out_to_period

# The most slots the planner tracks as cancellable: the cancelled set is a bit mask. Must equal PK_CANCEL_MAX_SLOTS in
# prediction_kernel.cpp (it sizes PkContext's cancel arrays), so a change to either needs a KERNEL_ABI_VERSION bump
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

    A slot qualifies when its car is cancellable, or force-held and the slot forced, it is not a car_charging_now_slots
    entry (those come first in the model list, now_slot_counts of them), it has energy and it ends
    after now. held_until maps car_n to an absolute minute; a slot ending by then is forced. One that runs on
    past the hold's end is not: it is published whole when cancelled, and the charger's hold extends into any
    cancelled slot under way at its end, so forcing it would let the hold carry itself on for as long as
    Octopus keeps extending the dispatch. An export over it may still cancel it, and so extend the hold.
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
            forced = hold_end is not None and slot["end"] <= hold_end
            if not forced and car_n not in cancellable_cars:
                continue
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


class CarCancelTable:
    """
    The per-step lookups a prediction needs to cancel car slots, built once per plan by build_car_cancel_table().

    Step k covers absolute minute minutes_now + k * PREDICT_STEP.

    - slots: the cancellable slots; bit j of a mask is slots[j].
    - forced_mask: the bits of the slots a charger is force-holding off, cancelled whatever the plan does.
    - slot_flat: car_n * n_steps + k -> the j of the slot in_car_slot() picks for that car at that step, or -1
      when the slot it picks is not cancellable (or there is none).
    - need_mask: per step, the bits of the cancellable dispatches covering it (rounded out to half hours).
    - trusted: per step, 1 when a dispatch that cannot be cancelled covers it, so its rate stays cheap.
    - revert_rate: per step, rate_max_base where a cancelled dispatch would go back to peak, else -1.
    - penalty_rate: what each cancelled kWh costs to buy back, rate_max_base.
    """

    __slots__ = ("slots", "forced_mask", "n_steps", "slot_flat", "need_mask", "trusted", "revert_rate", "penalty_rate")

    def __init__(self, slots, forced_mask, n_steps, slot_flat, need_mask, trusted, revert_rate, penalty_rate):
        self.slots = slots
        self.forced_mask = forced_mask
        self.n_steps = n_steps
        self.slot_flat = slot_flat
        self.need_mask = need_mask
        self.trusted = trusted
        self.revert_rate = revert_rate
        self.penalty_rate = penalty_rate


def step_range(minutes_now, n_steps, start, end):
    """The steps whose first minute falls in [start, end), clipped to the horizon."""
    first = max(-(-(start - minutes_now) // PREDICT_STEP), 0)
    last = min(-(-(end - minutes_now) // PREDICT_STEP), n_steps)
    return range(first, last)


def build_car_cancel_table(base, car_slots_model, slots):
    """
    The cancel table for this plan, or None when no slot can be cancelled.

    car_slots_model is the list the prediction models (car_charging_slots_model()), which slots index into.
    A dispatch is a slot marked octopus. A step reverts to rate_max_base only when it is io_adjusted, outside
    the fixed IOG window, not in a saving session and dispatches are not billed by window (the static half,
    held in revert_rate), and no uncancellable dispatch covers it while every cancellable one that does is
    cancelled (decided per prediction by step_reverted()).
    """
    if not slots:
        return None
    minutes_now = base.minutes_now
    n_steps = base.forecast_minutes // PREDICT_STEP
    num_cars = base.num_cars
    slot_j = {(slot.car_n, slot.index): j for j, slot in enumerate(slots)}
    forced_mask = 0
    for j, slot in enumerate(slots):
        if slot.forced:
            forced_mask |= 1 << j

    slot_flat = [-1] * (num_cars * n_steps)
    need_mask = [0] * n_steps
    trusted = [0] * n_steps
    for car_n in range(num_cars):
        car_slots = car_slots_model[car_n] if car_n < len(car_slots_model) else []
        offset = car_n * n_steps
        # Painted last slot first, so where slots overlap the first in model order wins, as in in_car_slot()
        for index in range(len(car_slots) - 1, -1, -1):
            slot = car_slots[index]
            j = slot_j.get((car_n, index), -1)
            for k in step_range(minutes_now, n_steps, slot["start"], slot["end"]):
                slot_flat[offset + k] = j
        for index, slot in enumerate(car_slots):
            if not slot.get("octopus", False):
                continue
            start, end = round_out_to_period(slot["start"], slot["end"])
            j = slot_j.get((car_n, index), -1)
            if j >= 0:
                bit = 1 << j
                for k in step_range(minutes_now, n_steps, start, end):
                    need_mask[k] |= bit
            elif dp2(slot.get("kwh", 0)) > 0:
                for k in step_range(minutes_now, n_steps, start, end):
                    trusted[k] = 1

    penalty_rate = float(base.rate_max_base)
    revert_rate = [-1.0] * n_steps
    if not base.dispatch_billing_window():
        io_adjusted = base.io_adjusted
        saving_minutes = base.rate_import_saving_minutes
        for k in range(n_steps):
            minute = minutes_now + k * PREDICT_STEP
            if io_adjusted.get(minute, False) and not base.in_iog_fixed_window(minute) and minute not in saving_minutes:
                revert_rate[k] = penalty_rate
    return CarCancelTable(slots, forced_mask, n_steps, slot_flat, need_mask, trusted, revert_rate, penalty_rate)


def step_reverted(table, k, mask):
    """Whether step k's dispatch rate goes back to peak: it can revert, nothing trusted covers it, and every dispatch covering it is cancelled."""
    need = table.need_mask[k]
    return table.revert_rate[k] >= 0 and not table.trusted[k] and need != 0 and not (need & ~mask)
