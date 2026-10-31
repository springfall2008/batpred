# Cancel a planned car slot to export — design

Status: draft for review, 2026-10-30

## Intent

Some export opportunities are worth far more than charging the car at that moment — an Octopus Saving
Session paying £1-£3/kWh is the motivating case. Today Predbat can never take one if a car charging slot
overlaps it: with `car_charging_from_battery` off, any export window that touches a car slot is
disallowed outright (`allow_this_export_window()` in `plan.py`, `export_window_allowed()` in the levels
pass, and the freeze-export pass all check `hit_car_window()`).

This feature lets the detailed planner cancel a pre-planned car slot so that an export can run over it,
but only when the export is clearly worth more than the car energy given up. The cancellation then
becomes real: a Predbat-led car simply does not charge in that slot, and an Octopus Intelligent car is
held off by suspending smart charging for the length of the slot.

## Goals

- A new switch, off by default, that allows the detailed pass to cancel car slots for export.
- A cancellation only wins when it is genuinely valuable, judged pessimistically: the car energy given up
  is valued as if it had to be bought back at the highest base import rate.
- The comparison is fair: a cancelled trial pays the import rate the house would really pay in that slot.
- Cancellations are reflected in the published car plan.
- Octopus Intelligent cars are physically held off during a cancelled slot and released afterwards,
  robustly (restarts, failed calls).

## Non-goals

- The levels pass and freeze export are unchanged — they keep today's hard block.
- No attempt to re-plan the car around a cancellation (move the energy to a later window). The
  pessimistic valuation stands in for that; the car plan is rebuilt every cycle anyway.
- Cars whose slots are owned by another component (e.g. Ohme's own schedule, `car_slot_owner` not Predbat
  or Octopus) are not cancelled; they keep the hard block.
- Changing the Octopus target SoC/time as a cancel mechanism (rejected — see below).

## Anti-goals

- Routine cancellation for ordinary export rates. If this fires on a normal day, the valuation is wrong.
- A car left uncharged because a hold was never released (restart mid-hold, a failed resume call).
- Predbat's own suspend being mistaken for the user removing the car — the device dropping out of
  `num_cars` and the car slots being rewired mid-hold.
- A cancellation that looks free because no time is left before the ready time.

## Decisions taken

### Switch

New config item `car_charging_cancel_for_export` (switch, default `False`), defined in `config.py`
alongside the other car items. *(Name and placement are my call; friendly name e.g. "Allow export to
cancel a car charging slot".)* When off, behaviour is byte-for-byte today's.

### Cancellation is a property of the plan, derived, not stored

With the switch on, **every** prediction applies one rule: an export window set to a full export (not
idle, not freeze) that touches a slot of a cancellable car cancels that slot. The cancel set is derived
from the plan's export windows each time, never chosen once and carried. *(My call.)* So the trials in
the optimiser, the final published prediction, `should_replace_plan()`'s re-scoring of the old plan,
and plans reused without re-optimising (`calculate_plan_every`, or restored after a restart) all see the
same cancellations. Nothing extra needs saving with the plan, and a reused plan can never keep an export
while losing its cancellation.

**Which passes may open such a window:** `allow_this_export_window()` stops rejecting a window that only
hits cancellable car slots. That gate is shared by the detailed pass and the refinement passes after it
(`optimise_plan_pass()`, `optimise_solar()`, `optimise_swap_export()`). Those passes refine the detailed
plan rather than levelling it, so they get the relaxed gate as well. *(My call.)* The levels pass
(`export_window_allowed()`) and freeze export keep today's hard block, as the user asked.

**Whole slots only.** An export window that touches a car slot cancels the whole slot, because the
Octopus hold stops the car for the whole slot anyway. *(My call.)*

**Cancellable cars** are only the ones Predbat can really stop:
- Predbat-led cars (slots from `plan_car_charging()`, `octopus: False`).
- Octopus Intelligent devices on Predbat's own Octopus component (its API session), where the hold below
  can reach them.

`octopus: True` slots from the Octopus Energy HA integration or from the Kraken component (E.ON/EDF
SmartFlex) can't be held off, so they are not cancellable and keep the hard block. Any other
`car_slot_owner` (such as Ohme) is not cancellable either. *(My call: cancelling a car Predbat can't hold
off would let it charge during the export.)*

### Valuing the cancelled energy (decided with the user)

In a trial that cancels slots:

1. The car load for those slots is removed, so the prediction's `cost` naturally drops by roughly
   kWh × slot rate (the car no longer buys that energy).
2. `metric_keep` is increased by **cancelled kWh × `rate_max_base`** — the highest *base* import rate
   over the forecast (`rate_base_min_max()` in `fetch.py`: no IO dispatch overlay, no saving/free/Axle
   sessions, no overrides).

The net effect on the metric is kWh × (`rate_max_base` − slot rate): the cost of having to buy that
energy back at peak. The slot rate must be subtracted **once only** — via the cost drop — so
`metric_keep` adds the full `rate_max_base`, not the difference.

`rate_max_base` is used rather than "the highest rate between the slot and the car's ready time" because
that window can be empty or all-cheap when the slot runs up to the ready time, making the cancellation
look free while the car leaves short. Using the horizon peak is deliberately pessimistic: a car with
plenty of time left pays the same penalty as one that would genuinely have to top up at peak. With a 7p
slot and a 30p peak, an export has to beat ~23p/kWh net to win — saving sessions clear that, normal
export rates do not.

Being score-only (`metric_keep`), the penalty never appears in reported cost.

### Fair rates in the cancelled slot (decided with the user)

An Octopus Intelligent dispatch outside the fixed off-peak window makes the house's import cheap only
because the dispatch happens. When a prediction cancels such a slot, the slot's minutes go **back to
peak**, as the user put it. This follows the rule `dynamic_load_car_strip_feed_rates()` already uses for
a dispatch it has cancelled (`octopus.py`):

- A minute is set to `rate_max_base` only when it is all of these: in the cancelled slot, from now
  onwards, outside the fixed 23:30-05:30 window, and marked `io_adjusted`.
- It stays cheap if a dispatch of another car that is not cancelled covers it, because every car shares
  the same rates.
- A saving/free/Axle session rate on that minute still wins over the reverted rate.

*(Settled from the user's "back to peak" and the existing precedent. `rate_import_no_io` was considered
and rejected: when the rate feed itself delivers the dispatch discount, that snapshot still holds the
cheap price, so nothing would be reverted.)*

Consequences that fall out:
- A slot inside the fixed off-peak window keeps its rate, since it is cheap by tariff, not by dispatch.
- Predbat-led slots on tariffs with no dispatch overlay have no `io_adjusted` minutes, so nothing changes.
- The window billing rule (`dispatch_billing_window()`, GH#5521) only applies to Kraken/E.ON. Those cars
  are not cancellable, so the case can't arise.

### The prediction kernel

Car slots and import rates are currently baked into the prediction context, both in Python
(`Prediction`) and the C++ kernel (`prediction_kernel.py` / `prediction_kernel.cpp`, built from
`pred.car_charging_slots` and `io_adjusted`). Because the cancel set follows from the export windows
passed to each prediction call, both engines need to work it out per call. For each cancelled slot they
zero the car load, revert the rate for its minutes, and add the `metric_keep` penalty. One way to do it is
to precompute in the context, for each cancellable slot, its kWh, its reverted-rate minutes and its
penalty, so each call only has to test window overlap. Python and kernel must produce identical results,
and the existing parity tests must cover the new input. Kernel binaries for every platform must be rebuilt (`build_kernel.sh` /
`build_kernel_cross.sh`) and verified (`verify_kernel_binary.py`).

The design of that per-trial input (e.g. a bitmask over car slots, a per-minute car-load scale plus rate
override array) is left to the builder, with the constraint that the switch off / no cancellation path
must not slow the optimiser down measurably — the detailed pass runs a very large number of trials.

### Publishing the cancellation

After the plan is chosen, each car slot touched by an exporting window is marked cancelled in
`car_charging_slots`, following the existing `dynamic_load_car_check()` pattern: `kwh` set to 0, the
original kept in `kwh_cancelled`, `cost` set to 0. The plan display shows it as cancelled (for example
with the existing "?"/cancelled styling or a distinct marker — builder's choice). Additionally record a
reason (e.g. `cancelled_for_export: True`) so the Octopus hold can find it.

For a Predbat-led car this is the whole mechanism: `binary_sensor.predbat_car_charging_slot` stays off for
that slot, so the charger does not start.

The car plan is rebuilt from scratch every cycle (`plan_car_charging()` in `fetch.py`), so each cycle must
re-justify the cancellation. If it stops being worth it, it falls away by itself. *(Accepted as correct
behaviour.)* Hysteresis: the existing plan-replacement rules (`should_replace_plan()`,
`metric_min_improvement_plan`) apply as normal.

### Octopus Intelligent hold (decided with the user)

New functions in `octopus.py` to suspend and unsuspend smart charging for one device. The mechanism is
**suspending smart charging**; reducing the target SoC/time was considered and rejected (moving the ready
time alone would not stop the charge, and the SoC would have to be dropped low enough to stop charging
entirely, triggering an unpredictable reschedule).

- **When:** in the 5 minutes before a cancelled slot starts (driven from the 15-second loop, as
  `dynamic_load_car_poll()` is, so it is not up to 5 minutes late), suspend the device. If the plan
  cancels a slot that has already started, suspend immediately. *(Mid-slot behaviour is my call.)*
- **Resume:** when the cancelled slot (or the contiguous run of cancelled slots) ends, unsuspend.
- **A hold, once started, is committed (decided with the user).** After the suspend has gone out, the
  hold stays until its planned resume time, even if a later plan no longer cancels the slot: Octopus
  will already have withdrawn the dispatches, and releasing early could let it reschedule into the
  export. While held, the car plan keeps showing the slot as cancelled. If the plan also cancels the very
  next slot before the resume time, the hold extends to cover it. Before the suspend goes out, the plan
  can change its mind freely.
- **Temporary suspension is not suspension.** While Predbat holds a device, `automatic_config()` /
  `get_active_intelligent_device_ids()` must treat it as active: it stays in `num_cars`, keeps its slot
  wiring, and does not trigger "Live intelligent devices changed".
- **Dispatches frozen.** While held, the device's planned dispatches are frozen at the last set seen
  before the suspend and are not re-read from Octopus. Octopus is expected to withdraw all of them on
  suspend and publish a fresh schedule on resume; after the resume, normal reads resume.
- **Slot confirmation paused.** `dynamic_load_car_check()` must not cancel the held car's slots for "in a
  dispatch but not charging" while the hold is in place.
- **Persistence.** The hold (device, start, planned resume time) is saved, so after a restart Predbat
  either continues it or resumes immediately if the resume time has passed.
- **Safety resume.** A hold is never allowed to outlast its planned resume time plus a margin, even if a
  resume call failed. The unsuspend is retried until it lands. *(The margin is the builder's choice; that
  a safety resume exists is my call.)*
- **Only Predbat's own suspends are undone.** A device the user suspended themselves is never resumed by
  Predbat.

The exact GraphQL mutation for suspend/unsuspend is not yet in `octopus.py`
(`setDevicePreferences` is used for targets; the existing code only *reads* `status.isSuspended`). The
builder must find it from the Octopus API schema, not from sample code.

**Build order (decided with the user):**
1. Build the suspend/unsuspend functions first, with a way to call them by hand.
2. Live-test them on the user's car against a real dispatch. The user has an 18:30 car charge to try it
   on. Confirm that suspend stops the car charging and the car does **not** fall back to charging
   straight away. Also record what happens to the other planned dispatches, how long a fresh schedule
   takes to appear after unsuspend, and whether the `isSuspended` read reflects the change.
3. Only then build the planner and hold work for Octopus cars.

If suspending leaves the car charging, or makes it charge at once, Octopus cars drop out of this feature
and only Predbat-led cars are cancellable until another mechanism is found. The Predbat-led half doesn't
depend on the test and can go ahead either way.

## Constraints

- Switch off ⇒ no behaviour change and no measurable optimiser slowdown.
- Unit tests for all new code (repo rule), including Python/kernel parity for the cancelled-slot input,
  the valuation (cancellation wins on a saving-session rate, loses on a normal export rate, does not look
  free when the slot ends at the ready time), the rate reversion cases (dispatch outside off-peak,
  inside off-peak, another car's live dispatch on the same minute, saving session overlap), the cancel
  set derived identically in trials, the final prediction, the old-plan re-score and a reused/restored
  plan, HA-integration and Kraken cars never cancelled, car-plan publishing, and the Octopus
  hold lifecycle (suspend timing, frozen dispatches, device kept active, restart mid-hold, failed
  resume retried, user's own suspension left alone).
- Tests share one `PredBat` fixture: restore any state the new tests change.
- Docs: document the switch in `docs/` (car charging and Octopus Intelligent pages), British English.

## Left to the builder

- The per-trial cancellation representation in Python and the kernel.
- Exactly how the plan display marks a slot cancelled for export.
- The safety-resume margin and retry cadence.
- How the hold state is stored (use the Storage component, not direct file access).
