# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Predbat-led EV charger control, shared by the charger components.

Each charger component used to carry its own copy of the same loop: read the car
charging plan, start the charger inside a planned window and stop it outside one,
put it back when something else changes it, and hand it back when Predbat is put in
read only mode or its control switch is turned off. CarChargerControl owns that loop
once; a component supplies only what is specific to its charger.
"""

from utils import parse_car_plan_windows, in_car_plan_window


class CarChargerControl:
    """Mixin that drives a component's EV chargers from Predbat's per-car charging plan.

    The component calls charger_control_setup() from initialize(), decides for itself
    whether control may run (setting charger_control_active), and calls
    charger_control_tick() from its run loop.

    It supplies the charger-specific parts:

    - charger_control_chargers(): (key, handle) pairs in car order, so the Nth charger
      follows car N. The key must be stable and hashable; the handle is passed back to
      the methods below.
    - charger_control_send(handle, charge, car_n): turn the charger on or off.
    - charger_control_release_one(handle, charge): hand a charger Predbat last set to
      charge back to its owner.

    and may override:

    - charger_control_connected(handle): False leaves the charger alone this cycle.
    - charger_control_drifted(handle, charge): True when the charger is no longer in the
      state Predbat last set, so it is set again.
    - charger_control_car_count(): how many cars have a plan to follow.

    Chargers with no car to follow are left alone rather than stopped: a charger whose
    car has no plan would otherwise read as "not planned" and be stopped mid-charge. One
    Predbat was already holding when its car went away is released rather than stranded.

    send and release_one must raise when the charger refuses, so nothing is recorded as
    done and the next cycle tries again; the component catches it in its run loop.
    """

    def charger_control_setup(self, log_name, noun, storage_module=None, storage_key=None, storage_field=None):
        """Initialise the shared control state.

        Args:
            log_name: How the component names itself in log lines, e.g. "myenergi".
            noun: What the chargers are called in log lines, e.g. "Zappi".
            storage_module, storage_key, storage_field: Where the control switch is persisted.
                Left as None the component has no switch and only read only mode releases.
        """
        self.charger_control_log_name = log_name
        self.charger_control_noun = noun
        self.charger_control_storage = (storage_module, storage_key, storage_field) if storage_module else None
        self.charger_control_active = False
        # The runtime switch, on unless the user turns it off. Restored from storage at startup.
        self.charger_control_enabled = True
        # Why control was last released, None while Predbat holds the chargers
        self.charger_control_released = None
        # car_n -> [(start, end)] from the last plan read
        self.charger_control_windows = {}
        # charger key -> True/False, the state Predbat last set. Only chargers Predbat has moved are here.
        self.charger_control_state = {}

    def charger_control_connected(self, handle):
        """Is a car on the cable - chargers that cannot tell are always treated as connected."""
        return True

    def charger_control_drifted(self, handle, charge):
        """Has the charger moved away from the state Predbat last set - never, unless the charger can tell."""
        return False

    def charger_control_car_count(self):
        """How many cars have a plan to follow."""
        return self.num_cars

    def charger_control_read_only_now(self):
        """Is Predbat in read only mode - the live attribute rather than just the config arg.

        Other components force read only by setting the attribute without touching the arg
        (axle_control, for one), so read the attribute first and fall back to the arg for
        the window before it is set.
        """
        read_only = getattr(self.base, "set_read_only", None)
        if read_only is None:
            read_only = self.get_arg("set_read_only", False)
        return bool(read_only)

    async def charger_control_set_enabled(self, enabled):
        """Handle the control switch being turned on or off, and persist it."""
        self.charger_control_enabled = bool(enabled)
        self.log("Info: {}: {} control switched {}".format(self.charger_control_log_name, self.charger_control_noun, "on" if self.charger_control_enabled else "off"))
        await self.charger_control_save_enabled()

    async def charger_control_save_enabled(self):
        """Persist the control switch so an off survives a restart.

        Without this a restart would silently take back a charger the user had deliberately
        released, which they would only notice when the car charged at the wrong time.
        Fails soft: no Storage component just means the switch is not sticky.
        """
        if self.charger_control_storage is None or self.storage is None:
            return
        module, key, field = self.charger_control_storage
        try:
            await self.storage.save(module, key, {field: self.charger_control_enabled})
        except Exception as exc:
            self.log("Warn: {}: could not save the {} control switch state: {}".format(self.charger_control_log_name, self.charger_control_noun, exc))

    async def charger_control_load_enabled(self):
        """Restore the control switch from storage, leaving it on when nothing is saved.

        Called before the first control cycle: the switch has to carry its restored state
        from the start, or a restart with control switched off would take the charger back
        for a cycle and then hand it over again.
        """
        if self.charger_control_storage is None or self.storage is None:
            return
        module, key, field = self.charger_control_storage
        try:
            saved = await self.storage.load(module, key)
        except Exception as exc:
            self.log("Warn: {}: could not read the {} control switch state: {}".format(self.charger_control_log_name, self.charger_control_noun, exc))
            return
        if isinstance(saved, dict) and field in saved:
            self.charger_control_enabled = bool(saved[field])
            if not self.charger_control_enabled:
                self.log("Info: {}: {} control is switched off from the last session".format(self.charger_control_log_name, self.charger_control_noun))

    def charger_control_refresh_windows(self, now):
        """Read Predbat's planned car charging windows for every car into charger_control_windows.

        The slot sensor's own on/off state only refreshes on Predbat's 5 minute cycle, so the
        planned attribute is parsed and judged against the clock here instead - otherwise
        every window boundary would be acted on up to 5 minutes late.

        Returns True once at least one car's plan has been read, False while no slot sensor
        has ever been published - which is what stops a restart stopping a charge before
        Predbat has decided anything.
        """
        windows = {}
        found = False
        for car_n in range(self.charger_control_car_count()):
            postfix = "" if car_n == 0 else "_{}".format(car_n)
            planned = self.get_state_wrapper("binary_sensor.{}_car_charging_slot{}".format(self.prefix, postfix), attribute="planned")
            if planned is None:
                continue
            found = True
            windows[car_n] = parse_car_plan_windows(planned, now, self.local_tz)
        self.charger_control_windows = windows
        return found

    def charger_control_should_charge(self, car_n, now):
        """Is now inside one of the planned charging windows for this car."""
        return in_car_plan_window(self.charger_control_windows.get(car_n, []), now)

    async def charger_control_tick(self, now):
        """Run one cycle of charger control, releasing rather than just going quiet.

        Read only mode and the control switch are both releases: Predbat may have left a
        charger stopped, and walking away from that would strand the car unable to charge.
        A component stop deliberately does not release, as that is nearly always a restart
        and releasing would glitch an in-progress charge.

        The caller passes now so every charger is judged against one instant.
        """
        if not self.charger_control_active:
            return
        reason = None
        if self.charger_control_read_only_now():
            reason = "Read only mode"
        elif not self.charger_control_enabled:
            reason = "The {} control switch".format(self.charger_control_noun)
        if reason:
            if self.charger_control_released is None:
                self.log("Info: {}: releasing the {} because of: {}".format(self.charger_control_log_name, self.charger_control_noun, reason))
                await self.charger_control_release()
                self.charger_control_released = reason
            return
        if self.charger_control_released is not None:
            self.log("Info: {}: {} cleared, resuming {} control".format(self.charger_control_log_name, self.charger_control_released, self.charger_control_noun))
            self.charger_control_released = None
        await self.charger_control_apply(now)

    async def charger_control_release(self):
        """Hand back every charger Predbat has moved, then forget them."""
        for key, handle in self.charger_control_chargers():
            if key not in self.charger_control_state:
                continue
            await self.charger_control_release_one(handle, self.charger_control_state[key])
        self.charger_control_state = {}

    async def charger_control_apply(self, now):
        """Drive every charger from its car's plan: on inside a planned window, off outside one.

        Predbat holds the charger for as long as it is in control, and puts it back if it has
        drifted from what was last set - purely edge-triggered control diverges silently once
        anything else touches the charger. A charger with no car connected is left alone.
        """
        if not self.charger_control_refresh_windows(now):
            return
        chargers = self.charger_control_chargers()
        car_count = self.charger_control_car_count()
        # A charger Predbat holds whose car has gone (num_cars dropped) would otherwise never be
        # commanded again, left charging or stopped with nobody in control - hand it back instead
        for key, handle in chargers[car_count:]:
            if key in self.charger_control_state:
                self.log("Info: {}: {} {} no longer has a car to follow, releasing it".format(self.charger_control_log_name, self.charger_control_noun, key))
                await self.charger_control_release_one(handle, self.charger_control_state[key])
                del self.charger_control_state[key]
        for car_n, (key, handle) in enumerate(chargers[:car_count]):
            if not self.charger_control_connected(handle):
                continue
            charge = self.charger_control_should_charge(car_n, now)
            last = self.charger_control_state.get(key, None)
            drifted = self.charger_control_drifted(handle, charge)
            if last == charge and not drifted:
                continue
            if last == charge:
                self.log("Info: {}: {} {} was changed away from what Predbat set, re-applying".format(self.charger_control_log_name, self.charger_control_noun, key))
            await self.charger_control_send(handle, charge, car_n)
            self.charger_control_state[key] = charge
