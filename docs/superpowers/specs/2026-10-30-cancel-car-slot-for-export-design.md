# Cancel a planned car slot to export — design

Status: draft for review, 2026-10-30. Revised 2026-10-31 after a live test: the hold moves from
Octopus to the charger.

## Intent

Some export opportunities are worth far more than charging the car at that moment — an Octopus Saving
Session paying £1-£3/kWh is the motivating case. Today Predbat can never take one if a car charging slot
overlaps it: with `car_charging_from_battery` off, any export window that touches a car slot is
disallowed outright (`allow_this_export_window()` in `plan.py`, `export_window_allowed()` in the levels
pass, and the freeze-export pass all check `hit_car_window()`).

This feature lets the detailed planner cancel a pre-planned car slot so that an export can run over it,
but only when the export is clearly worth more than the car energy given up. The cancellation then
becomes real:
- A Predbat-led car simply doesn't charge in that slot.
- A car whose slots someone else plans, such as Octopus Intelligent, is held off **at the charger**. The
  charger component force-pauses it for the slot and hands it back afterwards. The supplier's smart
  control is left on throughout.

## Goals

- A new switch, off by default, that allows the detailed pass to cancel car slots for export.
- A cancellation only wins when it is genuinely valuable, judged pessimistically: the car energy given up
  is valued as if it had to be bought back at the highest base import rate.
- The comparison is fair: a cancelled trial pays the import rate the house would really pay in that slot.
- Cancellations are reflected in the published car plan.
- Cars on a supplier's smart charging are physically held off during a cancelled slot and released
  afterwards, robustly (restarts, failed calls).
- A general "force hold" capability on the charger components, so each charger can join by implementing it.
  Wallbox first, then Zappi and Ohme, others as their mechanisms are found.

## Non-goals

- The levels pass and freeze export are unchanged — they keep today's hard block.
- No attempt to re-plan the car around a cancellation (move the energy to a later window). The
  pessimistic valuation stands in for that; the car plan is rebuilt every cycle anyway.
- Suspending Octopus smart control. It was live-tested and doesn't hold the car (see "Live test").
- Changing the Octopus target SoC/time as a cancel mechanism (rejected).
- Chargers without a force-hold implementation: their cars keep the hard block until one is written.

## Anti-goals

- Routine cancellation for ordinary export rates. If this fires on a normal day, the valuation is wrong.
- A car left uncharged because a hold was never released (restart mid-hold, a failed resume call).
- A charger left in a changed state: OCPP left off, or a Zappi left in Stopped. Octopus may get confused
  if a charger stays that way.
- Undoing a setting the user made themselves, such as turning OCPP back on when they had it off.
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

**Whole slots only.** An export window that touches a car slot cancels the whole slot, because a hold
stops the car for the whole slot anyway. *(My call.)*

**Cancellable cars (decided with the user)** are the ones Predbat can really stop:
- **Predbat-led cars** (slots from `plan_car_charging()`, `octopus: False`). Their own car plan drives the
  charger, so leaving the slot out is enough.
- **Any car whose charger component can force-hold it**, whoever plans its slots: Predbat's Octopus
  component, the Octopus Energy HA integration, or Kraken (E.ON/EDF SmartFlex). The charger does the
  stopping, not the supplier.

A car with neither keeps the hard block. Which charger serves which car is the charger component's own
car mapping (`CarChargerControl`, see below).

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
- Under the window billing rule (`dispatch_billing_window()`, GH#5521, Kraken/E.ON), the supplier bills
  the whole planned dispatch off-peak whether the car charges or not, so the rate is **not** reverted.
  Kraken cars are cancellable now, through their charger, so this case is live and needs a test.

### The prediction kernel

Car slots and import rates are currently baked into the prediction context, both in Python
(`Prediction`) and the C++ kernel (`prediction_kernel.py` / `prediction_kernel.cpp`, built from
`pred.car_charging_slots` and `io_adjusted`). Because the cancel set follows from the export windows
passed to each prediction call, both engines need to work it out per call. For each cancelled slot they
zero the car load, revert the rate for its minutes, and add the `metric_keep` penalty. One way to do it is
to precompute in the context, for each cancellable slot, its kWh, its reverted-rate minutes and its
penalty, so each call only has to test window overlap. Python and kernel must produce identical results,
and the existing parity tests must cover the new input. Kernel binaries for every platform must be
rebuilt (`build_kernel.sh` / `build_kernel_cross.sh`) and verified (`verify_kernel_binary.py`).

The design of that per-trial input (e.g. a bitmask over car slots, a per-minute car-load scale plus rate
override array) is left to the builder, with the constraint that the switch off / no cancellation path
must not slow the optimiser down measurably — the detailed pass runs a very large number of trials.

### Publishing the cancellation

After the plan is chosen, each car slot touched by an exporting window is marked cancelled in
`car_charging_slots`, following the existing `dynamic_load_car_check()` pattern: `kwh` set to 0, the
original kept in `kwh_cancelled`, `cost` set to 0. The plan display shows it as cancelled (for example
with the existing "?"/cancelled styling or a distinct marker — builder's choice). Additionally record a
reason (e.g. `cancelled_for_export: True`) so the charger hold can find it.

For a Predbat-led car this is the whole mechanism: `binary_sensor.predbat_car_charging_slot` stays off for
that slot, so the charger does not start.

The car plan is rebuilt from scratch every cycle (`plan_car_charging()` in `fetch.py`), so each cycle must
re-justify the cancellation. If it stops being worth it, it falls away by itself. *(Accepted as correct
behaviour.)* Hysteresis: the existing plan-replacement rules (`should_replace_plan()`,
`metric_min_improvement_plan`) apply as normal.

### The force hold on the charger (decided with the user)

**The interface.** `CarChargerControl` (`car_charger_control.py`) is the shared base of the charger
components: Wallbox, myenergi, Hypervolt, Ohme, and GivEnergy's charger in `gecloud.py`. It gains an
optional force-hold capability:
- a "can force-hold" check, which defaults to no;
- `force_pause()`: stop the charger now, overriding whoever is driving it;
- `force_resume()`: undo exactly what `force_pause()` changed, and leave whoever was driving it to
  restart the charge.

A component that doesn't implement it keeps its cars uncancellable. Hold state is saved through the
existing `charger_control_extra_state()` / `charger_control_restore_extra()` persistence, not a new store.

**Shared hold rules** (the base class owns these, each charger supplies only the two actions):
- **When:** in the 5 minutes before a cancelled slot starts, force-pause. This is driven from the 15-second
  loop, as `dynamic_load_car_poll()` is, so it is never up to 5 minutes late. If the plan cancels a slot
  that has already started, force-pause immediately. *(Mid-slot behaviour is my call.)*
- **Resume:** when the cancelled slot (or the contiguous run of cancelled slots) ends, force-resume.
- **A hold, once started, is committed (decided with the user).** After the force pause has gone out, the
  hold stays until its planned resume time, even if a later plan no longer cancels the slot. While held,
  the car plan keeps showing the slot as cancelled. If the plan also cancels the very next slot before the
  resume time, the hold extends to cover it. Before the force pause goes out, the plan can change its mind
  freely.
- **Verify, don't assume.** After a force pause, read the charger back and repeat the pause until it
  reports stopped. After a force resume, read it back and repeat until the charger reports it has been
  handed back. Each charger defines what "stopped" and "handed back" look like.
- **Persistence.** The hold is saved: car, charger, start, planned resume time, and the charger's
  pre-hold state (what `force_resume()` has to put back). After a restart Predbat continues the hold, or
  resumes at once if the resume time has passed.
- **Safety resume.** A hold is never allowed to outlast its planned resume time plus a margin, even if
  the plan has changed or a resume call failed. The resume is retried until it lands. *(The margin is the
  builder's choice; that a safety resume exists is my call.)*
- **Only undo our own changes.** `force_resume()` restores only what `force_pause()` changed. If the user
  had OCPP off already, or a Zappi already in Stopped, Predbat leaves it that way.
- **Slot confirmation paused.** During a hold the car sits idle inside a live dispatch, so
  `dynamic_load_car_check()` must not cancel the held car's slots for "in a dispatch but not charging".
- **The supplier is not touched.** Octopus smart control stays on, the device stays active, and its
  dispatches are read as normal. In the live test Octopus kept every dispatch through the hold. It did
  re-cut one at the hold's start (19:00→05:30 became 19:00→19:05 + 19:05→05:30), so the published slots
  can change shape mid-hold and the hold must key on time, not on a slot's identity.

### Wallbox force hold (first implementation, decided with the user)

While Octopus drives a Wallbox charger over OCPP, the Wallbox cloud refuses pause and resume (HTTP 409),
which Predbat's existing `held_by_ocpp()` guard already knows. So the Wallbox component gets:

- **(a) An OCPP connection toggle.** A switch per charger that writes the charger's OCPP configuration
  (`v3/chargers/<id>/ocpp-configuration`, POST), changing `type` between `ocpp` and `wallbox` and keeping
  `address`, `chargePointIdentity` and `password` as they were read. The password is never logged.
- **(b) Force pause.** Read and keep the OCPP configuration. Switch `type` to `wallbox`, wait until the
  charger reports it has left OCPP mode, and only then send the pause (remote-action 2). Read back and
  re-pause until it reports paused (`status_id` 182, 0 W).
- **(c) Force resume.** Switch `type` back to `ocpp` only; no resume command is sent. Octopus restarts the
  charge itself once the link is back (`connectionStatus` 4). Retry the OCPP write on failure, including
  HTTP 502, and keep checking until the link reconnects.

Only a charger that was in OCPP mode when the hold began has its OCPP switched off and on. One that was
already in Wallbox mode (e.g. Predbat-led) just gets a plain pause and resume.

### Zappi force hold (second implementation)

The myenergi component already sets Zappi modes (`set_mode()`, with `ZAPPI_MODE_STOPPED`):
- **Force pause:** keep the current mode (Eco+, Eco or Fast) in the hold state, then set Stopped. Stopped
  overrides the supplier's active command.
- **Force resume:** set the kept mode back.

Leaving a Zappi in Stopped can confuse Octopus's scheduling, so the safety resume matters most here. This
needs its own live test before it's trusted.

### Ohme force hold (third implementation)

The Ohme component already has both calls: `async_pause_charge()` (`POST /v1/chargeSessions/<serial>/stop`,
shown on a Home Pro as "Charging Paused") and `async_resume_charge()` (`.../resume`).
- **Force pause:** pause the session.
- **Force resume:** resume it.

Ohme's own guidance warns that a session left paused for a long time can make Octopus and Ohme raise a
"failed session" or communication alert, because the car isn't taking power when told to. So, as with the
Zappi, the hold is kept short and the safety resume matters most. Open questions for its live test:
- Does Octopus's next instruction override the pause mid-slot?
- Does resume hand the session back to Octopus's schedule, or start a charge regardless?

This also covers cars whose slots the Ohme component owns (`car_slot_owner` `"ohme"`).

### Other chargers

Hypervolt and GivEnergy's charger follow when their force-pause mechanism is found and live-tested. Until
then their cars keep the hard block.

## Live test, 2026-10-31 (user's Wallbox Pulsar on Octopus Intelligent, OCPP)

**Octopus suspend: doesn't hold the car.** `updateDeviceSmartControl(input: {deviceId, action})`, with
`action` `SUSPEND` or `UNSUSPEND` (confirmed by schema introspection):
- 18:56: suspended. The device went `SMART_CONTROL_OFF`, `isSuspended` true.
- Octopus did **not** withdraw the planned dispatches.
- 19:01: the Pulsar was charging at full power anyway.
- Predbat's existing suspended-device handling dropped the car's Intelligent slots within a minute ("Live
  intelligent devices changed … to []") and fell back to its own car plan.
- 19:03: unsuspended, and Octopus control returned straight away.

**Wallbox pause under OCPP: refused.** HTTP 409 "the charger cannot do that in its current state".

**Wallbox with OCPP switched off: works.**
- 19:10:06: OCPP switched off (`type` `wallbox`). The write was accepted at once.
- 19:10:21: first pause. Accepted, but the charger hadn't finished leaving OCPP mode and kept charging.
- 19:12:02: second pause. The car stopped (`status_id` 182, 0 W). The Wallbox app and HA's Wallbox
  sensor kept showing "charging" for several minutes; the API's `last_sync` time is the trustworthy one.
- Octopus kept the device `SMART_CONTROL_IN_PROGRESS` and kept its dispatches throughout.
- 19:17:01: switching OCPP back on returned **HTTP 502**. The paused charger hadn't synced with the
  Wallbox cloud since 19:13:48.
- 19:21:39: retried. Accepted, with `connectionStatus` 2.
- 19:24:07: `connectionStatus` 4. Octopus restarted the charge at 7.06 kW **without any resume command**.

## Build order (decided with the user)

1. **Wallbox force hold**, in the Wallbox component: the OCPP toggle, force pause and force resume, with a
   way to call each by hand (the existing `wallbox.py` command line). Live re-test on the user's charger
   with the real code.
2. **The force-hold interface and shared hold rules** in `CarChargerControl`.
3. **The planner work:** switch, valuation, rates, kernel and publishing. The Predbat-led half depends on
   none of the above and can be built alongside.
4. **Zappi force hold**, then **Ohme force hold**, each with its own live test.

## Constraints

- Switch off ⇒ no behaviour change and no measurable optimiser slowdown.
- Unit tests for all new code (repo rule), including:
  - Python/kernel parity for the cancelled-slot input.
  - The valuation: cancellation wins on a saving-session rate, loses on a normal export rate, and doesn't
    look free when the slot ends at the ready time.
  - The rate reversion cases: dispatch outside off-peak, inside off-peak, another car's live dispatch on
    the same minute, saving-session overlap, and the window billing rule.
  - The cancel set derived identically in trials, the final prediction, the old-plan re-score and a
    reused/restored plan.
  - Cars with neither Predbat-led slots nor a force-hold charger never cancelled.
  - Car-plan publishing.
  - The hold lifecycle: pause timing, the commitment rule and extension, verify-and-repeat on both
    pause and resume, restart mid-hold, a failed resume retried (including a 502), the safety resume,
    and the user's own OCPP-off or Stopped setting left alone.
  - Wallbox: OCPP write keeps address, identity and password and never logs the password; pause only
    after the mode change has landed.
- Tests share one `PredBat` fixture: restore any state the new tests change.
- Docs: document the switch in `docs/` (car charging, Octopus Intelligent and the charger component
  pages), British English.

## Left to the builder

- The per-trial cancellation representation in Python and the kernel.
- Exactly how the plan display marks a slot cancelled for export.
- The safety-resume margin and retry cadence.
- How each charger reports "stopped" and "handed back" for the verify step, within what's described above.
