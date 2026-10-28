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

from datetime import datetime, timezone

from utils import parse_car_plan_windows, in_car_plan_window, strptime_aware


def parse_dispatch_time(value):
    """Parse a dispatch start or end from an Octopus or Kraken dispatch sensor, or None.

    Octopus writes "2026-06-01T01:00:00+0000", Kraken ISO with a trailing Z.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = strptime_aware(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    # A time with no offset cannot be compared with the clock; both sources write UTC
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# A guest switch left on turns itself off after this long, for chargers that cannot tell a car was unplugged
GUEST_CHARGING_MAX_HOURS = 12

# How long a charger is held as it is while the Octopus or Kraken component wires its Intelligent devices.
# Discovery normally finishes in the first cycle or two; past this, one that never gets there (a failed login,
# say) is treated as cannot tell - warned about once, and a charger Predbat holds is handed back
OCTOPUS_DISCOVERY_WAIT_MINUTES = 15

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


class ChargerControlDeferred(Exception):
    """Raised by a component's charger_control_release_one() when the charger cannot be released yet.

    For a charger that has not been heard from, say. Unlike a failure it is not reported: the
    charger stays held and the release is tried again next cycle.
    """


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
    - charger_control_unplugged(handle): True when the charger reports its car unplugged, so a
      charger Predbat stopped is released then rather than holding the next car. Defaults to False.

    Chargers with no car to follow are left alone rather than stopped: a charger whose
    car has no plan would otherwise read as "not planned" and be stopped mid-charge. One
    Predbat was already holding when its car went away is released rather than stranded.

    The guest charging switch hands the chargers back so a car Predbat is not planning for
    can charge. It turns itself off when a car plugged in after it was switched on is
    unplugged again, on chargers that can tell, and otherwise after GUEST_CHARGING_MAX_HOURS.

    A car whose Octopus Intelligent dispatches are delivered by Octopus driving the charger
    itself is left to Octopus - see charger_control_octopus_drives_charger(). Where Octopus
    instead drives the car, the charger is left alone too, unless the user turns on
    octopus_intelligent_charger_follows_car - Predbat then drives the charger from the slot
    sensor that carries the Octopus dispatches, so the car cannot charge outside them on its
    own timers. See charger_control_octopus_drives_car().

    send and release_one must raise when the charger refuses, so nothing is recorded as
    done and the next cycle tries again; the component catches it in its run loop. A release
    that cannot happen yet raises ChargerControlDeferred instead, which keeps the charger held
    without being reported.

    For a component with storage, the chargers Predbat holds are saved with the control switch,
    so a restart can still release a charger an earlier session stopped. A stop is recorded
    before it is sent, as one whose reply is lost may still have been applied.
    """

    def charger_control_setup(self, log_name, noun, storage_module=None, storage_key=None, storage_field=None, control=None, switch_prefix=None, control_setting=None):
        """Initialise the shared control state.

        Args:
            log_name: How the component names itself in log lines, e.g. "myenergi".
            noun: What the chargers are called in log lines, e.g. "Zappi".
            storage_module, storage_key, storage_field: Where the control switch and the chargers
                Predbat holds are persisted - for a component with no switch (Ohme), just the latter.
                Left as None neither survives a restart.
            control: The component's control setting from apps.yaml. None (unset) lets control
                run wherever the component's automatic setup maps chargers to cars; False turns
                it off; True also drives a charger whose Octopus arrangement cannot be told.
            switch_prefix: The component's entity name, e.g. "gecloud", for its guest charging
                switch. Left as None the component has no guest switch.
            control_setting: The apps.yaml name of the control setting, e.g. "ge_cloud_evc_control",
                named in the warning when Predbat cannot tell whether Octopus drives a charger.
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
        # Saved with the control switch when the component has storage.
        self.charger_control_state = {}
        # Held chargers whose state is not confirmed - a stop that did not confirm, or one restored after a
        # restart - so the plan's command is sent again even though it matches what is held
        self.charger_control_unconfirmed = set()
        # A save of the control state that has not gone through yet
        self.charger_control_save_pending = False
        # Whether the saved control state has been read; a read that failed is tried again each tick
        self.charger_control_loaded = False
        self.charger_control_setting = control_setting
        # car_n -> why that car's charger is left alone ("octopus", "car", "discovering", "discovery_failed" or "unknown"), so
        # each reason is logged once rather than every cycle, and again only when it changes
        self.charger_control_octopus_cars = {}
        # car_n -> when that car began waiting for Octopus or Kraken discovery. Kept per car, so one car whose
        # driver is known cannot keep restarting the clock of another still waiting
        self.charger_control_discovery_since = {}
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

    def charger_control_unplugged(self, handle):
        """Does the charger report its car unplugged - never, unless the charger can tell."""
        return False

    def charger_control_car_count(self):
        """How many cars have a plan to follow."""
        return self.num_cars

    def charger_control_octopus_drives_charger(self, car_n):
        """Does Octopus Intelligent deliver this car's dispatches by driving its charger?

        True: Octopus switches the charger itself, so Predbat must leave it alone or the two
        would fight - whatever octopus_intelligent_charging says, as that switch only changes
        Predbat's own planning and Octopus goes on driving the charger regardless. False: there
        is no Octopus Intelligent car here, or Octopus drives the car rather than the charger -
        see charger_control_octopus_drives_car() for what Predbat does then. None: the car is on
        Octopus Intelligent but which device Octopus drives cannot be told, or not yet.

        Read from the car's own wired dispatch sensor rather than from any one component:
        the Octopus and Kraken components both publish the device's is_charger on it, and
        reading it there cannot pair a car with some other car's device.
        """
        slot = self.charger_control_dispatch_sensor(car_n)
        if not slot:
            # Not wired for this car - but the Octopus component may simply not have got there yet
            return None if self.charger_control_octopus_discovering() else False
        if car_n == 0 and self.charger_control_slots_claimed():
            # Another charger component (Ohme) supplies car 0's Intelligent slots from the charger itself
            return True
        return self.charger_control_dispatch_is_charger(slot)

    def charger_control_slots_claimed(self):
        """Has another charger component (Ohme) claimed car 0's Intelligent car wiring.

        The claim is for car 0 alone, the car that component wires. Since #5443 it covers only the
        ready time and charge limit: the Ohme supplies the planned slots, and the Octopus component
        goes on wiring its own dispatch sensors into octopus_intelligent_slot, one per device. So a
        second car has a sensor of its own and is judged on what that sensor says.
        """
        owner = getattr(self.base, "car_slot_owner", None)
        return bool(owner and owner != "octopus")

    def charger_control_dispatch_is_charger(self, slot):
        """What a dispatch sensor says about whether its Intelligent device is the charger: True, False or None.

        None for a sensor that does not say - not published yet, one that never does (the Octopus
        Energy HA integration, an Ohme slot sensor), or one that has lost its value, which is not
        the same as "not the charger".
        """
        is_charger = self.get_state_wrapper(slot, attribute="is_charger")
        if is_charger is None:
            return None
        if isinstance(is_charger, str) and is_charger.strip().lower() in ("unknown", "unavailable", "none", ""):
            return None
        return parse_control_setting(is_charger)

    def charger_control_octopus_drives_car(self, car_n):
        """Does Octopus Intelligent deliver this car's dispatches by driving the car itself?

        The car is the Intelligent device (a BMW or Volkswagen linked to Octopus, say), so the car
        starts and stops its own charge and the charger only supplies it. Predbat then leaves the
        charger alone unless switch.predbat_octopus_intelligent_charger_follows_car is on, in which
        case it drives the charger to the dispatches so the car cannot also charge on its own timers.
        """
        slot = self.charger_control_dispatch_sensor(car_n)
        if not slot:
            return False
        if car_n == 0 and self.charger_control_slots_claimed():
            return False
        return self.charger_control_dispatch_is_charger(slot) is False

    def charger_control_octopus_discovering(self):
        """Is the Octopus or Kraken component still to wire its Intelligent devices into the car slots?

        Only a component running its automatic setup ever wires them, so one with
        octopus_automatic off is never "still discovering" - waiting on it would leave the
        charger alone for good. How long each car has waited is kept, see charger_control_discovery_overdue().
        """
        components = getattr(self.base, "components", None)
        discovering = False
        if components:
            octopus = components.get_component("octopus")
            # Kraken wires its SmartFlex devices in the same run that first succeeds - from its cache,
            # or from a fresh discovery - so until then its slots may simply not be there yet
            kraken = components.get_component("kraken")
            discovering = (octopus is not None and octopus.automatic and octopus.intelligent_config_devices is None) or (kraken is not None and not kraken.api_started)
        if not discovering:
            self.charger_control_discovery_since = {}
        return discovering

    def charger_control_discovery_overdue(self, car_n):
        """Has car car_n's wait for Octopus or Kraken discovery gone on past OCTOPUS_DISCOVERY_WAIT_MINUTES?

        The clock starts the first time this is asked for the car during a wait.

        Discovery normally finishes in a cycle or two. One that does not - a Kraken login that keeps
        failing, say - still cannot say whether Octopus drives the charger, so it is then treated as
        "cannot tell" rather than waited on for good.
        """
        since = self.charger_control_discovery_since.setdefault(car_n, datetime.now())
        return (datetime.now() - since).total_seconds() >= OCTOPUS_DISCOVERY_WAIT_MINUTES * 60

    def charger_control_log_left_alone(self, car_n, why):
        """Say why a car's charger is being left alone.

        Waiting for Octopus discovery normally clears within a cycle or two, so it is Info. A
        dispatch sensor that does not say which device Octopus drives never clears on its own,
        and leaves the charger uncontrolled, so it is a warning naming the setting that fixes it.
        """
        name = self.charger_control_log_name
        noun = self.charger_control_noun
        if why == "octopus":
            self.log("Info: {}: Octopus Intelligent drives car {}'s {}, leaving it to Octopus".format(name, car_n, noun))
        elif why == "car":
            self.log(
                "Info: {}: Octopus Intelligent drives car {} itself, leaving its {} alone - in expert mode, turn on switch.{}_octopus_intelligent_charger_follows_car for Predbat to drive the {} to the dispatches".format(
                    name, car_n, noun, self.prefix, noun
                )
            )
        elif why == "discovering":
            self.log("Info: {}: waiting for the Octopus or Kraken component to find its devices before controlling car {}'s {}".format(name, car_n, noun))
        elif why == "discovery_failed":
            setting = self.charger_control_setting or "the {} control setting".format(noun)
            self.log(
                "Warn: {}: the Octopus or Kraken component has not found its devices after {} minutes, so Predbat cannot tell whether Octopus Intelligent drives car {}'s {} and is leaving it alone. Check that component, or set {}: true in apps.yaml if the {} is not the Octopus device".format(
                    name, OCTOPUS_DISCOVERY_WAIT_MINUTES, car_n, noun, setting, noun
                )
            )
        else:
            setting = self.charger_control_setting or "the {} control setting".format(noun)
            self.log(
                "Warn: {}: cannot tell whether Octopus Intelligent drives car {}'s {} - its dispatch sensor does not say - so Predbat is leaving the {} alone. Set {}: true in apps.yaml if the {} is not the Octopus device".format(
                    name, car_n, noun, noun, setting, noun
                )
            )

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
        """Is Predbat in read only mode: the setting, or Axle's VPP read only.

        Not the base's set_read_only attribute: reset() leaves it True until the first update_pred(),
        which comes after a component's first runs, so it would release every charger Predbat holds on
        each restart. Axle forces read only by setting only the attribute, so its own flag is read too.
        """
        return bool(self.get_arg("set_read_only", False)) or bool(self.base.set_read_only_axle)

    async def charger_control_set_enabled(self, enabled):
        """Handle the control switch being turned on or off, and persist it."""
        self.charger_control_enabled = bool(enabled)
        self.log("Info: {}: {} control switched {}".format(self.charger_control_log_name, self.charger_control_noun, "on" if self.charger_control_enabled else "off"))
        await self.charger_control_save_enabled()

    async def charger_control_save_enabled(self):
        """Persist the control switch so an off survives a restart - see charger_control_save()."""
        await self.charger_control_save()

    async def charger_control_save(self):
        """Persist the control switch and the chargers Predbat holds.

        Without the switch a restart would silently take back a charger the user had deliberately
        released, which they would only notice when the car charged at the wrong time; without the
        held chargers it would forget one it had stopped, and could never release it.
        Fails soft: no Storage component just means neither survives a restart.
        """
        if self.charger_control_storage is None or self.storage is None:
            return
        module, key, field = self.charger_control_storage
        # Set until a save goes through, so a failed one is tried again even if nothing has changed since
        self.charger_control_save_pending = True
        try:
            # Storage reports serialisation and I/O failures by returning False rather than raising
            document = dict(self.charger_control_extra_state())
            document.update({field: self.charger_control_enabled, "held": dict(self.charger_control_state)})
            if await self.storage.save(module, key, document) is False:
                raise OSError("storage refused the save")
            self.charger_control_save_pending = False
        except Exception as exc:
            self.log("Warn: {}: could not save the {} control state, it may revert to the last saved state on restart: {}".format(self.charger_control_log_name, self.charger_control_noun, exc))

    def charger_control_extra_state(self):
        """Anything else of the component's own to save in the same document - none unless it says so."""
        return {}

    def charger_control_restore_extra(self, saved):
        """Restore what charger_control_extra_state() saved, from the loaded document - nothing unless the component says so."""

    async def charger_control_load_enabled(self):
        """Restore the control switch, and the chargers Predbat held, from storage; the switch is left on when nothing is saved.

        Returns False only when the read failed, so a component can try again next cycle.

        Called before the first control cycle: the switch has to carry its restored state
        from the start, or a restart with control switched off would take the charger back
        for a cycle and then hand it over again.
        """
        if self.charger_control_storage is None or self.storage is None:
            self.charger_control_loaded = True
            return True
        module, key, field = self.charger_control_storage
        try:
            saved = await self.storage.load(module, key)
        except Exception as exc:
            self.log("Warn: {}: could not read the {} control switch state: {}".format(self.charger_control_log_name, self.charger_control_noun, exc))
            return False
        self.charger_control_loaded = True
        if not isinstance(saved, dict):
            return True
        if field in saved:
            self.charger_control_enabled = bool(saved[field])
            if not self.charger_control_enabled:
                self.log("Info: {}: {} control is switched off from the last session".format(self.charger_control_log_name, self.charger_control_noun))
        held = saved.get("held")
        if isinstance(held, dict):
            # Merged under what this session already holds, which is newer
            for charger_key, charge in held.items():
                if str(charger_key) not in self.charger_control_state:
                    self.charger_control_state[str(charger_key)] = bool(charge)
                    # Not confirmed: the charger may have been changed while Predbat was down, and not every
                    # charger can tell, so the first cycle sends the plan's command again as it did before
                    self.charger_control_unconfirmed.add(str(charger_key))
        self.charger_control_restore_extra(saved)
        return True

    def charger_control_refresh_windows(self, now):
        """Read Predbat's planned car charging windows for every car into charger_control_windows.

        The slot sensor's own on/off state only refreshes on Predbat's 5 minute cycle, so the
        planned attribute is parsed and judged against the clock here instead - otherwise
        every window boundary would be acted on up to 5 minutes late.

        Returns True once at least one car's plan has been read, False while no slot sensor
        has ever been published - which is what stops a restart stopping a charge before
        Predbat has decided anything. A car whose own plan has not been published yet is left
        out of charger_control_windows, so it reads as unknown rather than as an empty plan; one
        whose plan has gone missing after being read keeps the windows last read.
        """
        windows = {}
        found = False
        for car_n in range(self.charger_control_car_count()):
            postfix = "" if car_n == 0 else "_{}".format(car_n)
            planned = self.get_state_wrapper("binary_sensor.{}_car_charging_slot{}".format(self.prefix, postfix), attribute="planned")
            if planned is None:
                # A plan read before and missing now (the sensor recreated, say) keeps its last
                # windows - they are absolute times - rather than turning into an empty plan that
                # would stop a car mid-charge
                if car_n in self.charger_control_windows:
                    windows[car_n] = self.charger_control_windows[car_n]
                    found = True
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

        Only reached for a charger Predbat drives: Octopus drives the car, or the charger and the
        user has Predbat filter it. The plan carries the dispatches too, but is only republished
        every 5 minutes and leaves out a dispatch Octopus has not given any energy yet - following
        the dispatch sensor as well stops the charger holding the car off for the first minutes of
        a new dispatch. With octopus_intelligent_charging off Predbat follows only its own plan.

        The planned dispatches come from octopus_intelligent_planned_slot where it is set, as they
        do for the car's plan, so a dispatch that sensor leaves out does not run the charger.

        The dispatch times on the sensor are judged against the clock rather than its on/off
        state, which only refreshes every couple of minutes and would run the charger on past
        the end of a dispatch. A sensor with no dispatch times falls back to its state.
        """
        if not self.get_arg("octopus_intelligent_charging", True):
            return False
        slot = self.charger_control_dispatch_sensor(car_n)
        if not slot:
            return False
        planned_slot = self.charger_control_planned_sensor(car_n, slot)
        reads = [(planned_slot or slot, "planned_dispatches"), (slot, "completed_dispatches")]
        if planned_slot:
            reads.append((planned_slot, "completed_dispatches"))
        dispatches = []
        for entity_id, attribute in reads:
            value = self.get_state_wrapper(entity_id, attribute=attribute)
            if isinstance(value, list):
                dispatches.extend(value)
        if not dispatches:
            # The slot sensor's state would put back the dispatches the planned sensor left out
            return False if planned_slot else self.get_state_wrapper(slot) == "on"
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
        slot = slots[car_n]
        # An apps.yaml default whose regex matched nothing (yet) is still its literal "re:" string - not an entity
        if not slot or (isinstance(slot, str) and slot.startswith("re:")):
            return None
        return slot

    def charger_control_planned_sensor(self, car_n, slot):
        """The sensor this car's planned dispatches come from in place of its slot sensor, or None.

        The same rule as Fetch.car_planned_slot_entity() and its caller: none set, a regex that matched
        nothing, the slot sensor itself, or octopus_intelligent_slot_action_config in use all leave the
        slot sensor as the plan.
        """
        if "octopus_intelligent_slot_action_config" in self.args:
            return None
        planned = self.get_arg("octopus_intelligent_planned_slot", None, indirect=False)
        if planned and not isinstance(planned, list):
            planned = [planned]
        if not planned or car_n >= len(planned):
            return None
        planned = planned[car_n]
        if not planned or not isinstance(planned, str) or planned.startswith("re:") or planned == slot:
            return None
        return planned

    async def charger_control_tick(self, now):
        """Run one cycle of charger control, releasing rather than just going quiet.

        Read only mode, the control switch and guest charging are all releases: Predbat may
        have left a charger stopped, and walking away from that would strand the car unable
        to charge - or leave the guest unable to.
        A component stop deliberately does not release, as that is nearly always a restart
        and releasing would glitch an in-progress charge.

        The caller passes now so every charger is judged against one instant.
        """
        if not self.charger_control_loaded:
            # A read that failed at start-up - or one a component never got to - is tried again, so a
            # charger an earlier session held is not forgotten for the rest of this one
            await self.charger_control_load_enabled()
        if self.charger_control_save_pending:
            # A save that failed is tried again each cycle, not only when a hold next changes
            await self.charger_control_save()
        if not self.charger_control_active:
            # A charger an earlier session held - control since turned off in apps.yaml - is still released.
            # Only for a component that ticks while control is off; one that does not leaves it held
            if self.charger_control_state:
                if self.charger_control_released is None:
                    self.log("Info: {}: releasing the {} because {} control is not enabled".format(self.charger_control_log_name, self.charger_control_noun, self.charger_control_noun))
                    self.charger_control_released = "Control not enabled"
                await self.charger_control_release_inactive()
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
                self.charger_control_released = reason
            # Every cycle while the reason holds, not just the first: a charger that refused, or could not
            # be released yet, is still held and is tried again. Once all are released this does nothing
            await self.charger_control_release()
            return
        if self.charger_control_released is not None:
            self.log("Info: {}: {} cleared, resuming {} control".format(self.charger_control_log_name, self.charger_control_released, self.charger_control_noun))
            self.charger_control_released = None
        await self.charger_control_apply(now)

    # Exceptions that stop charger_control_each() at once - an account-wide refusal such as a rate limit,
    # where going on to the next charger would only be refused too and the component must back off
    charger_control_fatal_errors = ()

    async def charger_control_each(self, steps):
        """Await each step in turn - one coroutine factory per charger - so one charger that
        refuses a command does not stop the others being handled. The first failure is raised
        once every step has had its turn, for the component's run loop to log and retry."""
        failure = None
        for step in steps:
            try:
                await step()
            except ChargerControlDeferred:
                # Kept held, and tried again next cycle without being reported
                continue
            except self.charger_control_fatal_errors:
                raise
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

    async def charger_control_release_inactive(self):
        """Hand back every held charger while control is off: to Octopus where Octopus now drives it, released otherwise.

        As the active path does - a plain release would start a charge Octopus is scheduling. A charger
        past the cars, whose car cannot be asked about, is released.
        """
        car_count = self.charger_control_car_count()
        steps = []
        for car_n, (key, handle) in enumerate(self.charger_control_chargers()):
            if key not in self.charger_control_state:
                continue
            if car_n < car_count and self.charger_control_octopus_drives_charger(car_n) is True:
                steps.append(lambda key=key, handle=handle: self.charger_control_hand_over_held(key, handle))
            else:
                steps.append(lambda key=key, handle=handle: self.charger_control_release_held(key, handle))
        await self.charger_control_each(steps)

    async def charger_control_hand_over_held(self, key, handle):
        """Hand one held charger to Octopus, then forget it - after, so a failed command is retried."""
        await self.charger_control_hand_to_octopus(handle, self.charger_control_state[key])
        await self.charger_control_forget(key)

    async def charger_control_forget(self, key):
        """Stop holding a charger, and save that."""
        del self.charger_control_state[key]
        self.charger_control_unconfirmed.discard(key)
        await self.charger_control_save()

    async def charger_control_hold(self, key, charge):
        """Hold a charger in the state Predbat set, saving only when that changes."""
        changed = self.charger_control_state.get(key) != charge
        self.charger_control_state[key] = charge
        if changed:
            # A save that failed earlier is retried at the start of each cycle, see charger_control_tick()
            await self.charger_control_save()

    async def charger_control_hand_over_all(self):
        """Hand every held charger to Octopus, each on its own so one refusing does not strand the rest."""
        held = [(key, handle) for key, handle in self.charger_control_chargers() if key in self.charger_control_state]
        await self.charger_control_each([lambda key=key, handle=handle: self.charger_control_hand_over_held(key, handle) for key, handle in held])

    async def charger_control_release_held(self, key, handle):
        """Release one charger Predbat holds, then forget it."""
        await self.charger_control_release_one(handle, self.charger_control_state[key])
        await self.charger_control_forget(key)

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
        if drives is not None:
            # Known either way, so a later wait for discovery starts its clock afresh
            self.charger_control_discovery_since.pop(car_n, None)
        # Octopus drives the car itself: the charger is left alone too, unless the user has asked
        # Predbat to drive it to the dispatches - or has turned Octopus Intelligent charging off in
        # Predbat, which then plans the car itself and drives the charger to that plan
        car_driven = drives is False and self.get_arg("octopus_intelligent_charging", True) and not self.get_arg("octopus_intelligent_charger_follows_car", False) and self.charger_control_octopus_drives_car(car_n)
        # Octopus drives the charger, but control: true plus a planned sensor filtering its dispatches has Predbat stop it outside them
        filtered = drives is True and self.charger_control_config is True and not (car_n == 0 and self.charger_control_slots_claimed()) and self.charger_control_planned_sensor(car_n, self.charger_control_dispatch_sensor(car_n)) is not None
        # An explicit control: true is the user saying their charger is not the Octopus
        # device, so it overrides "cannot tell" - but never a known charge point
        if (drives is True and not filtered) or car_driven or (drives is None and self.charger_control_config is not True):
            if drives:
                why = "octopus"
            elif car_driven:
                why = "car"
            elif self.charger_control_octopus_discovering():
                why = "discovery_failed" if self.charger_control_discovery_overdue(car_n) else "discovering"
            else:
                why = "unknown"
            if self.charger_control_octopus_cars.get(car_n) != why:
                self.charger_control_log_left_alone(car_n, why)
                self.charger_control_octopus_cars[car_n] = why
            if key in self.charger_control_state:
                if why == "octopus":
                    # Handed over before it is forgotten, so a failed command is retried next cycle
                    await self.charger_control_hand_over_held(key, handle)
                elif why != "discovering":
                    # Nobody is known to be taking it over - with Octopus driving the car, nobody drives the
                    # charger - so a stop is undone rather than left in place
                    await self.charger_control_release_held(key, handle)
                # While discovery is under way the charger stays as it is, and held, so it is settled
                # either way once discovery has finished - releasing would start a charge Predbat had stopped
            return
        if car_n in self.charger_control_octopus_cars:
            self.log("Info: {}: car {} is no longer left to Octopus, Predbat drives the {}".format(self.charger_control_log_name, car_n, self.charger_control_noun))
            del self.charger_control_octopus_cars[car_n]
        if self.charger_control_unplugged(handle):
            if key in self.charger_control_state:
                # Handed back when the car goes: a stop that outlived the unplug would hold the next car,
                # and a charger left allowed would charge it outside the plan
                self.log("Info: {}: car {} was unplugged from {} {}, releasing it".format(self.charger_control_log_name, car_n, self.charger_control_noun, key))
                await self.charger_control_release_held(key, handle)
            # An empty charger is not driven - stopping it now would only be released again next cycle
            return
        if not self.charger_control_connected(handle):
            return
        if car_n not in self.charger_control_windows and (key not in self.charger_control_state or key in self.charger_control_unconfirmed) and not self.charger_control_dispatch_active(car_n, now):
            # This car's plan has not been published yet - one car's slot sensor can appear before
            # another's - and no dispatch says to charge, so Predbat knows nothing to act on: an
            # absent plan is not an empty one, and stopping the charger would cut off a charge.
            # A charger Predbat already holds is still driven, so one a dispatch started is stopped
            # when the dispatch ends rather than left running with nobody in control - but not one only
            # restored after a restart, which waits for its plan like any other
            return
        charge = self.charger_control_should_charge(car_n, now)
        last = self.charger_control_state.get(key, None)
        drifted = self.charger_control_drifted(handle, charge)
        unconfirmed = key in self.charger_control_unconfirmed
        if last == charge and not drifted and not unconfirmed:
            return
        if last == charge and not unconfirmed:
            self.log("Info: {}: {} {} was changed away from what Predbat set, re-applying".format(self.charger_control_log_name, self.charger_control_noun, key))
        if charge:
            await self.charger_control_send(handle, charge, car_n)
            self.charger_control_unconfirmed.discard(key)
            await self.charger_control_hold(key, charge)
            return
        # A stop is recorded, and saved, before it is sent: one whose reply is lost may still have been
        # applied, and must then be released like any other. Until it confirms it is sent again each cycle
        self.charger_control_unconfirmed.add(key)
        await self.charger_control_hold(key, False)
        await self.charger_control_send(handle, charge, car_n)
        self.charger_control_unconfirmed.discard(key)
