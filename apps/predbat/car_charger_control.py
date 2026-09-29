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

from datetime import datetime

from utils import parse_car_plan_windows, in_car_plan_window


def parse_dispatch_time(value):
    """Parse a dispatch start or end from an Octopus or Kraken dispatch sensor, or None.

    Octopus writes "2026-06-01T01:00:00+0000", Kraken ISO with a trailing Z.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


# A guest switch left on turns itself off after this long, for chargers that cannot tell a car was unplugged
GUEST_CHARGING_MAX_HOURS = 12

# The strings get_arg() reads as true for a boolean setting
CONTROL_TRUE_STRINGS = ("on", "true", "yes", "enabled", "enable", "connected")


def parse_control_setting(value):
    """Normalise a charger's control setting from apps.yaml to None, True or False.

    The settings have no default, so that unset can mean "follow the automatic setup" - but
    that also skips get_arg()'s boolean conversion, which only runs against a boolean
    default. Without this a quoted "false", or a 0, would read as set and not False.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in CONTROL_TRUE_STRINGS
    return bool(value)


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
    - charger_control_car_plugged(handle): False once a car is unplugged, which ends guest
      charging. Defaults to charger_control_connected().
    - charger_control_hand_to_octopus(handle, charge): let go of a charger Octopus is taking
      over, without starting a charge.

    Chargers with no car to follow are left alone rather than stopped: a charger whose
    car has no plan would otherwise read as "not planned" and be stopped mid-charge. One
    Predbat was already holding when its car went away is released rather than stranded.

    The guest charging switch hands the chargers back so a car Predbat is not planning for
    can charge. It turns itself off when a car plugged in after it was switched on is
    unplugged again, on chargers that can tell, and otherwise after GUEST_CHARGING_MAX_HOURS.

    A car whose Octopus Intelligent dispatches are delivered by Octopus driving the charger
    itself is left to Octopus - see charger_control_octopus_drives_charger(). Where Octopus
    instead drives the car, Predbat still drives the charger, from the slot sensor that then
    carries the Octopus dispatches, so the car cannot charge outside them on its own timers.

    send and release_one must raise when the charger refuses, so nothing is recorded as
    done and the next cycle tries again; the component catches it in its run loop.
    """

    def charger_control_setup(self, log_name, noun, storage_module=None, storage_key=None, storage_field=None, control=None, switch_prefix=None):
        """Initialise the shared control state.

        Args:
            log_name: How the component names itself in log lines, e.g. "myenergi".
            noun: What the chargers are called in log lines, e.g. "Zappi".
            storage_module, storage_key, storage_field: Where the control switch is persisted.
                Left as None the component has no switch and only read only mode releases.
            control: The component's control setting from apps.yaml. None (unset) lets control
                run wherever the component's automatic setup maps chargers to cars; False turns
                it off; True also drives a charger whose Octopus arrangement cannot be told.
            switch_prefix: The component's entity name, e.g. "gecloud", for its guest charging
                switch. Left as None the component has no guest switch.
        """
        self.charger_control_config = control
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
        # Cars currently left to Octopus, so the hand-over is logged once rather than every cycle
        self.charger_control_octopus_cars = set()
        self.charger_control_switch_prefix = switch_prefix
        # Guest charging: deliberately not persisted, so a restart puts Predbat back in charge
        self.charger_control_guest = False
        # When the first control cycle saw guest charging on; the chargers seen empty since, and those
        # a car has then been plugged in to - only an unplug from one of those ends guest charging
        self.charger_control_guest_since = None
        self.charger_control_guest_empty = set()
        self.charger_control_guest_connected = set()

    def charger_control_connected(self, handle):
        """Is a car on the cable - chargers that cannot tell are always treated as connected."""
        return True

    def charger_control_drifted(self, handle, charge):
        """Has the charger moved away from the state Predbat last set - never, unless the charger can tell."""
        return False

    def charger_control_car_plugged(self, handle):
        """Is a car plugged in, for ending guest charging - kept apart from charger_control_connected().

        A charger can report its plug state without Predbat also skipping commands to it while
        it is empty, which is what charger_control_connected() returning False would mean.
        """
        return self.charger_control_connected(handle)

    def charger_control_car_count(self):
        """How many cars have a plan to follow."""
        return self.num_cars

    def charger_control_octopus_drives_charger(self, car_n):
        """Does Octopus Intelligent deliver this car's dispatches by driving its charger?

        True: Octopus switches the charger itself, so Predbat must leave it alone or the two
        would fight - whatever octopus_intelligent_charging says, as that switch only changes
        Predbat's own planning and Octopus goes on driving the charger regardless. False: there
        is no Octopus Intelligent car here, or Octopus drives the car rather than the charger -
        Predbat then drives the charger, from the dispatches or, with octopus_intelligent_charging
        off, from its own plan. None: the car is on Octopus Intelligent but which device Octopus
        drives cannot be told, or not yet.

        Read from the car's own wired dispatch sensor rather than from any one component:
        the Octopus and Kraken components both publish the device's is_charger on it, and
        reading it there cannot pair a car with some other car's device.
        """
        slot = self.charger_control_dispatch_sensor(car_n)
        if not slot:
            # Not wired for this car - but the Octopus component may simply not have got there yet
            return None if self.charger_control_octopus_discovering() else False
        owner = getattr(self.base, "car_slot_owner", None)
        if owner and owner != "octopus":
            # Another charger component (Ohme) supplies the Intelligent slots from the charger itself
            return True
        is_charger = self.get_state_wrapper(slot, attribute="is_charger")
        if is_charger is None:
            # Not published yet, or a sensor that does not say (the Octopus Energy HA integration)
            return None
        return parse_control_setting(is_charger)

    def charger_control_octopus_discovering(self):
        """Is the Octopus component still to wire its Intelligent devices into the car slots?

        Only a component running its automatic setup ever wires them, so one with
        octopus_automatic off is never "still discovering" - waiting on it would leave the
        charger alone for good.
        """
        components = getattr(self.base, "components", None)
        octopus = components.get_component("octopus") if components else None
        if octopus is None or not octopus.automatic:
            return False
        return octopus.intelligent_config_devices is None

    async def charger_control_hand_to_octopus(self, handle, charge):
        """Let go of a charger Octopus is taking over, without starting a charge.

        A normal release undoes a stop so a car is never stranded, but here Octopus decides
        when the charger runs - starting it would charge outside its dispatches. A charger
        Predbat had left running is released as usual; one it had stopped is left for Octopus.
        """
        if charge:
            await self.charger_control_release_one(handle, charge)

    def charger_control_guest_entity(self):
        """The guest charging switch's entity id, or None for a component without one."""
        if self.charger_control_switch_prefix is None:
            return None
        return "switch.{}_{}_guest_charging".format(self.prefix, self.charger_control_switch_prefix)

    def charger_control_publish_guest(self, app):
        """Publish the guest charging switch - called only while control is active."""
        entity = self.charger_control_guest_entity()
        if entity is None:
            return
        self.dashboard_item(
            entity,
            state="on" if self.charger_control_guest else "off",
            attributes={"friendly_name": "{} Guest Charging".format(self.charger_control_noun), "icon": "mdi:account-arrow-right"},
            app=app,
        )

    async def charger_control_guest_event(self, entity_id, service):
        """Handle the guest charging switch, returning True when entity_id was it."""
        if self.charger_control_switch_prefix is None or not entity_id.endswith("_{}_guest_charging".format(self.charger_control_switch_prefix)):
            return False
        self.charger_control_set_guest(not self.charger_control_guest if service == "toggle" else service == "turn_on")
        return True

    def charger_control_set_guest(self, on, why=None):
        """Turn guest charging on or off, starting its unplug and time limits afresh."""
        self.charger_control_guest = bool(on)
        self.charger_control_guest_since = None
        self.charger_control_guest_empty = set()
        self.charger_control_guest_connected = set()
        self.log("Info: {}: guest charging switched {}{}".format(self.charger_control_log_name, "on" if on else "off", " - {}".format(why) if why else ""))

    def charger_control_guest_over(self, now):
        """Why guest charging should end now, or None while it should carry on.

        A charger only ends it by going empty, then plugged in, then empty again after it was
        switched on. A car already on a charger at that point may be the owner's, unplugged to
        make way for the guest, so its unplug must not end it - the cost is that a guest who
        plugged in first is left to the time limit. Chargers that cannot tell always read as
        plugged in, which also leaves them to the time limit.
        """
        if self.charger_control_guest_since is None:
            self.charger_control_guest_since = now
        if (now - self.charger_control_guest_since).total_seconds() >= GUEST_CHARGING_MAX_HOURS * 3600:
            return "on for {} hours".format(GUEST_CHARGING_MAX_HOURS)
        for key, handle in self.charger_control_chargers()[: self.charger_control_car_count()]:
            if not self.charger_control_car_plugged(handle):
                if key in self.charger_control_guest_connected:
                    return "the guest's car was unplugged from {} {}".format(self.charger_control_noun, key)
                self.charger_control_guest_empty.add(key)
            elif key in self.charger_control_guest_empty:
                self.charger_control_guest_connected.add(key)
        return None

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
        """Is now inside one of the planned charging windows for this car, or an Octopus dispatch."""
        return in_car_plan_window(self.charger_control_windows.get(car_n, []), now) or self.charger_control_dispatch_active(car_n, now)

    def charger_control_dispatch_active(self, car_n, now):
        """Is an Octopus Intelligent dispatch running for this car right now?

        Only reached for a charger Predbat drives, so here Octopus drives the car. The plan
        carries the dispatches too, but is only republished every 5 minutes and leaves out a
        dispatch Octopus has not given any energy yet - following the dispatch sensor as well
        stops the charger holding the car off for the first minutes of a new dispatch. With
        octopus_intelligent_charging off Predbat follows only its own plan.

        The dispatch times on the sensor are judged against the clock rather than its on/off
        state, which only refreshes every couple of minutes and would run the charger on past
        the end of a dispatch. A sensor with no dispatch times falls back to its state.
        """
        if not self.get_arg("octopus_intelligent_charging", True):
            return False
        slot = self.charger_control_dispatch_sensor(car_n)
        if not slot:
            return False
        dispatches = []
        for attribute in ("planned_dispatches", "completed_dispatches"):
            value = self.get_state_wrapper(slot, attribute=attribute)
            if isinstance(value, list):
                dispatches.extend(value)
        if not dispatches:
            return self.get_state_wrapper(slot) == "on"
        for dispatch in dispatches:
            if not isinstance(dispatch, dict):
                continue
            start = parse_dispatch_time(dispatch.get("start"))
            end = parse_dispatch_time(dispatch.get("end"))
            if start and end and start <= now < end:
                return True
        return False

    def charger_control_dispatch_sensor(self, car_n):
        """The Octopus Intelligent dispatch sensor wired to this car, or None."""
        slots = self.get_arg("octopus_intelligent_slot", None, indirect=False)
        if slots and not isinstance(slots, list):
            slots = [slots]
        if not slots or car_n >= len(slots):
            return None
        return slots[car_n] or None

    async def charger_control_tick(self, now):
        """Run one cycle of charger control, releasing rather than just going quiet.

        Read only mode, the control switch and guest charging are all releases: Predbat may
        have left a charger stopped, and walking away from that would strand the car unable
        to charge - or leave the guest unable to.
        A component stop deliberately does not release, as that is nearly always a restart
        and releasing would glitch an in-progress charge.

        The caller passes now so every charger is judged against one instant.
        """
        if not self.charger_control_active:
            return
        if self.charger_control_guest:
            why = self.charger_control_guest_over(now)
            if why:
                self.charger_control_set_guest(False, why)
        reason = None
        if self.charger_control_read_only_now():
            reason = "Read only mode"
        elif not self.charger_control_enabled:
            reason = "The {} control switch".format(self.charger_control_noun)
        elif self.charger_control_guest:
            reason = "Guest charging"
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

    async def charger_control_each(self, steps):
        """Await each step in turn - one coroutine factory per charger - so one charger that
        refuses a command does not stop the others being handled. The first failure is raised
        once every step has had its turn, for the component's run loop to log and retry."""
        failure = None
        for step in steps:
            try:
                await step()
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    async def charger_control_release(self):
        """Hand back every charger Predbat has moved, forgetting each once it is released.

        A charger that refuses stays held, so the next cycle retries just that one.
        """
        held = [(key, handle) for key, handle in self.charger_control_chargers() if key in self.charger_control_state]
        await self.charger_control_each([lambda key=key, handle=handle: self.charger_control_release_held(key, handle) for key, handle in held])

    async def charger_control_release_held(self, key, handle):
        """Release one charger Predbat holds, then forget it."""
        await self.charger_control_release_one(handle, self.charger_control_state[key])
        del self.charger_control_state[key]

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
        steps = [lambda key=key, handle=handle: self.charger_control_car_gone(key, handle) for key, handle in chargers[car_count:]]
        steps += [lambda car_n=car_n, key=key, handle=handle: self.charger_control_drive_one(car_n, key, handle, now) for car_n, (key, handle) in enumerate(chargers[:car_count])]
        await self.charger_control_each(steps)

    async def charger_control_car_gone(self, key, handle):
        """Release a charger Predbat holds whose car has gone (num_cars dropped).

        It would otherwise never be commanded again, left charging or stopped with nobody in control.
        """
        if key in self.charger_control_state:
            self.log("Info: {}: {} {} no longer has a car to follow, releasing it".format(self.charger_control_log_name, self.charger_control_noun, key))
            await self.charger_control_release_held(key, handle)

    async def charger_control_drive_one(self, car_n, key, handle, now):
        """Drive one charger from car car_n's plan, or leave it to Octopus."""
        drives = self.charger_control_octopus_drives_charger(car_n)
        # An explicit control: true is the user saying their charger is not the Octopus
        # device, so it overrides "cannot tell" - but never a known charge point
        if drives is True or (drives is None and self.charger_control_config is not True):
            if car_n not in self.charger_control_octopus_cars:
                if drives:
                    self.log("Info: {}: Octopus Intelligent drives car {}'s {}, leaving it to Octopus".format(self.charger_control_log_name, car_n, self.charger_control_noun))
                else:
                    self.log("Info: {}: cannot tell yet whether Octopus Intelligent drives car {}'s {}, leaving it alone".format(self.charger_control_log_name, car_n, self.charger_control_noun))
                self.charger_control_octopus_cars.add(car_n)
            if key in self.charger_control_state:
                # Handed over before it is forgotten, so a failed command is retried next cycle
                await self.charger_control_hand_to_octopus(handle, self.charger_control_state[key])
                del self.charger_control_state[key]
            return
        if car_n in self.charger_control_octopus_cars:
            self.log("Info: {}: car {} is no longer left to Octopus, Predbat drives the {}".format(self.charger_control_log_name, car_n, self.charger_control_noun))
            self.charger_control_octopus_cars.discard(car_n)
        if not self.charger_control_connected(handle):
            return
        charge = self.charger_control_should_charge(car_n, now)
        last = self.charger_control_state.get(key, None)
        drifted = self.charger_control_drifted(handle, charge)
        if last == charge and not drifted:
            return
        if last == charge:
            self.log("Info: {}: {} {} was changed away from what Predbat set, re-applying".format(self.charger_control_log_name, self.charger_control_noun, key))
        await self.charger_control_send(handle, charge, car_n)
        self.charger_control_state[key] = charge
