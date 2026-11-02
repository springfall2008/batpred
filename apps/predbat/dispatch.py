# pylint: disable=line-too-long
"""
Real-time dispatch control for inverters that expose a Remote Dispatch API.

Some inverters cannot honour scheduled export depth (a firmware-locked discharge target
SOC, for example) but do expose a real-time dispatch interface: a RAM-only control block
the inverter obeys immediately, with an inverter-side failsafe that self-reverts if no
fresh command arrives within a configurable interval. wills106/homeassistant-solax-modbus
exposes this for Solis hybrid inverters (44100 block, protocol Ver3.4); other integrations
can follow the same pattern.

Predbat drives dispatch through optional per-inverter entities named in apps.yaml.
Because the block is RAM-only, the design point is *cycle ownership*: while Predbat is
inside an export window it re-applies the dispatch command every cycle, which both
keeps the failsafe fed and corrects drift; the failsafe is the safety net if Predbat or
Home Assistant dies mid-window. Outside a window the dispatch is disabled, so a dead
Predbat reverts the inverter to demand mode on its own.

The command itself is applied from execute_plan() *after* the cycle's rates have been
written (see Execute.apply_dispatch_intent), not from the export-window writer, so the
power it carries is the rate this cycle chose rather than the previous cycle's.

Optional apps.yaml args (dispatch_power, dispatch_apply_button and dispatch_control_mode
are required, plus at least one of dispatch_disable_button / dispatch_master; the rest are
optional):

    dispatch_apply_button:      button that block-writes the staged dispatch block
    dispatch_control_mode:      select holding the dispatch mode (see dispatch_mode)
    dispatch_power:             number holding the dispatch power in W, signed
                                (negative = discharge / export on Solis)
    dispatch_disable_button:    button that stops dispatch and clears the block
    dispatch_master:            switch enabling the dispatch block (one of this and
                                dispatch_disable_button is required)
    dispatch_soc_min:           number holding the dispatch SOC floor %
    dispatch_soc_max:           number holding the dispatch SOC ceiling % (optional)
    dispatch_failsafe_interval: number holding the failsafe window in minutes (optional)
    dispatch_failsafe_minutes:  value to arm it with (default 10; never less than two
                                execution cycles)
    dispatch_status:            sensor reporting whether the inverter-side block is
                                running (optional)
    dispatch_mode:              select option used for an export (default
                                "Battery Charge" - the Solis name for charge/discharge
                                by the sign of the power)

Convention: discharge/export commands are negative power. Predbat computes export
power as a positive discharge rate in W, so the sign is flipped on write.

A freeze export is deliberately NOT driven through dispatch: there is no dispatch command
that means "hold and do not export", so adjust_export_immediate() leaves freeze exports on
its stock path (which also issues the caller's discharge_freeze_service) and the dispatch
block is disabled instead, because no export rate is planned for the cycle.
"""

from const import RUN_EVERY

# Status readings that mean the inverter-side dispatch block is NOT running. Anything else is
# treated as hot, so an unrecognised status errs towards stopping an export rather than leaving
# one running. "Disabled"/"Stopped"/"Demand" are the inverter's own idle vocabulary and must not
# be read as active, or a quiet cycle would press Disable.
DISPATCH_IDLE_STATUS = ("idle", "off", "disabled", "stopped", "demand", "none", "", "0", "unknown", "unavailable")

# The args that must resolve to an entity for THIS inverter before dispatch can drive it.
DISPATCH_REQUIRED_ARGS = ("dispatch_power", "dispatch_apply_button", "dispatch_control_mode")


class InverterDispatch:
    """Per-inverter helper that stages and applies real-time dispatch commands."""

    def __init__(self, inverter):
        """Bind to the parent inverter and resolve the dispatch entities from apps.yaml."""
        self.inv = inverter
        self.base = inverter.base
        self.log = inverter.log
        # Whether a dispatch block may be running on the inverter, i.e. whether anything has to
        # be stopped when the window ends. Lives with the Inverter (this helper is built once in
        # __init__, not per cycle), so refresh_config() cannot reset it and leave a window running
        # past its end with nothing to disable it.
        self.active = False
        # Cached answer from enabled(), re-resolved by refresh() once per refresh_config().
        self._enabled = None
        # Warnings already emitted, keyed by the problem, so a per-cycle check does not spam.
        self._warned = set()
        # Last status reading we pressed Disable for, so a status sensor that has not refreshed
        # yet does not produce a second press next cycle.
        self._disabled_status = None

    def refresh(self):
        """Re-resolve the configuration: called from Inverter.refresh_config() once a cycle.

        enabled() is consulted several times per inverter per cycle (the force-export writer, the
        immediate-export writer and the apply pass), and resolving it can warn, so the answer is
        cached here rather than recomputed on every call.
        """
        self._enabled = None

    def enabled(self):
        """True when this inverter can actually be driven by dispatch.

        Checks that every required entity resolves for *this* inverter, not just that the args
        appear in apps.yaml: in a multi-inverter setup a single-value arg is shared by every
        inverter (so two of them would fight over one block), while a per-inverter list that is
        short leaves the later inverters with no entity at all. Either way the inverter cannot
        stage the block, so it must keep the scheduled-slot export path it can use - returning
        True on arg presence alone left it with neither.

        A dispatch that cannot be stopped is refused too: with neither a disable button nor a
        master switch configured, nothing Predbat does at the end of a window reaches the
        inverter, so the block would run until its failsafe expired.
        """
        if self._enabled is None:
            self._enabled = self._compute_enabled()
        return self._enabled

    def _compute_enabled(self):
        """Work out whether dispatch is usable for this inverter (the body of enabled())."""
        args = getattr(self.base, "args", None) or {}
        missing = [arg for arg in DISPATCH_REQUIRED_ARGS if not self._entity(arg)]
        if missing:
            # Silent when none of the args are set at all: that is the stock timed-slot setup,
            # not a dispatch misconfiguration, and this runs for every inverter every cycle.
            if any(args.get(arg) for arg in DISPATCH_REQUIRED_ARGS):
                self._warn_once(
                    ("missing",) + tuple(missing),
                    "Warn: Inverter {} dispatch: {} not set for this inverter, using the scheduled-slot export path".format(self.inv.id, ", ".join(missing)),
                )
            return False
        if not self._entity("dispatch_disable_button") and not self._entity("dispatch_master"):
            self._warn_once(
                ("no_stop",),
                "Warn: Inverter {} dispatch: neither dispatch_disable_button nor dispatch_master is set, so a running dispatch could not be stopped - using the scheduled-slot export path".format(self.inv.id),
            )
            return False
        shared = self._shared_args()
        if shared:
            self._warn_once(
                ("shared",) + tuple(shared),
                "Warn: Inverter {} dispatch: {} is shared with another inverter, and two inverters cannot drive one dispatch block - using the scheduled-slot export path".format(self.inv.id, ", ".join(shared)),
            )
            return False
        return True

    def _warn_once(self, key, message):
        """Emit a dispatch warning once per distinct problem."""
        if key not in self._warned:
            self._warned.add(key)
            self.log(message)

    def _entity(self, arg):
        """Return the entity_id for an arg on this inverter, or None.

        Reads the raw apps.yaml entry first so a per-inverter list that is too short, or a
        single-value arg seen by a later inverter, is handled here rather than by get_arg:
        get_arg(index=N) on a single value logs Predbat's own "incorrectly setup" warning, and a
        short list logs "Out of range index", both of which would repeat every cycle for a
        perfectly deliberate fallback to the scheduled-slot path.
        """
        args = getattr(self.base, "args", None) or {}
        raw = args.get(arg, None)
        if isinstance(raw, list):
            if self.inv.id >= len(raw):
                return None
            index = self.inv.id
        else:
            # A single value is shared by every inverter (refused by _shared_args when there is
            # more than one), so it is resolved at index 0 to keep get_arg quiet.
            index = 0
        entity_id = self.base.get_arg(arg, indirect=False, index=index)
        return entity_id if entity_id else None

    def _setting(self, arg, default):
        """Return a scalar dispatch setting, per inverter when given as a list."""
        args = getattr(self.base, "args", None) or {}
        raw = args.get(arg, None)
        if isinstance(raw, list):
            if self.inv.id >= len(raw):
                return default
            return self.base.get_arg(arg, default, index=self.inv.id)
        return self.base.get_arg(arg, default, index=0)

    def _shared_args(self):
        """Required dispatch args this inverter resolves to the same entity as another inverter."""
        inverters = getattr(self.base, "inverters", None) or []
        if len(inverters) < 2:
            return []
        shared = []
        for arg in DISPATCH_REQUIRED_ARGS:
            entity = self._entity(arg)
            for other in inverters:
                other_dispatch = getattr(other, "dispatch", None)
                other_entity = getattr(other_dispatch, "_entity", None)
                if other is self.inv or other_entity is None:
                    continue
                if other_entity(arg) == entity:
                    shared.append(arg)
                    break
        return shared

    def _write_and_poll(self, name, entity_id, value, fuzzy=0, required_unit=None):
        """Route a write through the inverter's write-and-poll helpers (ledger-aware)."""
        return self.inv.write_and_poll_value(name, entity_id, value, fuzzy=fuzzy, required_unit=required_unit)

    def _press_button(self, name, entity_id):
        """Press a button entity via HA services, mirroring _press_single_toggle_button."""
        result = self.base.call_service_wrapper("button/press", entity_id=entity_id)
        self.log("Inverter {} dispatch: pressed {} ({})".format(self.inv.id, name, entity_id))
        return result is not False

    def _dispatch_status(self):
        """The inverter-side dispatch status reading, as a lower-case string, or None."""
        status_entity = self._entity("dispatch_status")
        if not status_entity:
            return None
        try:
            state = self.base.get_state_wrapper(status_entity)
        except Exception:
            return None
        return None if state is None else str(state).strip().lower()

    def _status_is_hot(self, status):
        """Whether a status reading means the inverter-side block is running."""
        return status is not None and status not in DISPATCH_IDLE_STATUS

    def _failsafe_minutes(self):
        """Failsafe window to arm, in minutes, never shorter than two execution cycles.

        The failsafe is what stops the export if Predbat or Home Assistant dies mid-window, so it
        has to outlast the gap between the commands Predbat sends. Predbat re-applies dispatch
        every cycle, and a cycle is RUN_EVERY minutes - not plan_interval_minutes, which is the
        width of a plan slot and is 30 by default. Flooring against the plan slot armed a
        60-minute failsafe on a default install, so a dead Predbat could keep the battery
        exporting for an hour.
        """
        minutes = self._setting("dispatch_failsafe_minutes", 10)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 10
        return max(minutes, RUN_EVERY * 2)

    def _not_applied(self, message):
        """Log why this cycle's command was not applied, leaving `active` alone.

        A failed staging write says nothing about a block applied on a previous cycle: that one
        is still running on the inverter until a later Apply replaces it or the failsafe expires.
        Clearing `active` here was what let an export run past its window with nothing left to
        disable it, so the flag is only ever cleared by a confirmed Disable.
        """
        self.log(message)
        return False

    def apply_dispatch(self, power_w, soc_min=None, soc_max=None, mode=None):
        """Stage the dispatch parameters and apply the dispatch block.

        Stages mode/power/SOC bounds into the config entities, enables the master
        (when present), then presses the apply button. Safe to call every Predbat
        cycle while an export window is active: write-and-poll skips no-op writes,
        but the apply button is re-pressed each call to feed the failsafe.

        Returns True only when every parameter write verified and Apply was pressed. A value
        the inverter rejected must not be committed by a later Apply: it would run whatever
        was staged before, under a log line claiming the new power.

        Args:
            power_w: signed dispatch power in W (positive = charge, negative = discharge)
            soc_min: optional SOC floor % for the dispatch (None = leave staged)
            soc_max: optional SOC ceiling % for the dispatch (None = leave staged)
            mode:    dispatch mode name (default from dispatch_mode, else "Battery Charge" =
                     battery charge/discharge by power sign, on the Solis protocol)
        """
        if mode is None:
            mode = self._setting("dispatch_mode", "Battery Charge")
        power_entity = self._entity("dispatch_power")
        apply_entity = self._entity("dispatch_apply_button")
        if not power_entity or not apply_entity:
            return self._not_applied("Warn: Inverter {} dispatch: no dispatch power/apply entity configured, dispatch not applied".format(self.inv.id))

        # Master enable FIRST, unconditionally: never trust the HA switch state for
        # the master. The inverter clears 44100 itself on failsafe expiry or block
        # reset while Home Assistant keeps reporting the switch as "on" (optimistic
        # local state), and a stale-on read makes write_and_poll_switch skip the
        # write entirely - after which every realtime block write is silently
        # discarded by the master-gated inverter (dispatch no-ops with no error).
        # A forced turn_on is idempotent on the inverter (re-arms 44100), so always
        # issuing it costs one switch write per cycle. force=True keeps the write on the
        # normal write-and-poll path, so the control ledger and write accounting see it.
        master_entity = self._entity("dispatch_master")
        if master_entity:
            if not self.inv.write_and_poll_switch("dispatch_master", master_entity, True, force=True):
                return self._not_applied("Warn: Inverter {} dispatch: master switch did not confirm on, dispatch not applied".format(self.inv.id))

        # Stage the mode first (it selects which element the dispatch acts on). No local cache:
        # write_and_poll_option already skips a write that changes nothing, and remembering it
        # here would skip re-staging a mode the inverter had reset after a failsafe expiry.
        mode_entity = self._entity("dispatch_control_mode")
        if mode_entity and not self.inv.write_and_poll_option("dispatch_control_mode", mode_entity, mode):
            return self._not_applied("Warn: Inverter {} dispatch: mode {} did not take, dispatch not applied".format(self.inv.id, mode))

        # Stage the signed power (discharge negative)
        if not self._write_and_poll("dispatch_power", power_entity, int(power_w), required_unit="W"):
            return self._not_applied("Warn: Inverter {} dispatch: power {}W did not take, dispatch not applied".format(self.inv.id, int(power_w)))

        # Stage the SOC window
        for arg, value in (("dispatch_soc_min", soc_min), ("dispatch_soc_max", soc_max)):
            if value is None:
                continue
            entity_id = self._entity(arg)
            if entity_id and not self._write_and_poll(arg, entity_id, int(value), required_unit="%"):
                return self._not_applied("Warn: Inverter {} dispatch: {} {} did not take, dispatch not applied".format(self.inv.id, arg, int(value)))

        # Failsafe interval: re-armed every cycle, because scattered single-register writes only
        # commit while the master is enabled and an expired failsafe needs the value restored
        # before the next Apply. write-and-poll skips it while the value already matches.
        failsafe_entity = self._entity("dispatch_failsafe_interval")
        if failsafe_entity:
            failsafe = self._failsafe_minutes()
            if not self._write_and_poll("dispatch_failsafe_interval", failsafe_entity, failsafe, required_unit="min"):
                return self._not_applied("Warn: Inverter {} dispatch: failsafe interval {}min did not take, dispatch not applied".format(self.inv.id, failsafe))

        # Apply the block
        if not self._press_button("dispatch apply", apply_entity):
            return self._not_applied("Warn: Inverter {} dispatch: apply button did not press, dispatch not applied".format(self.inv.id))
        self.active = True
        self._disabled_status = None
        self.log("Inverter {} dispatch: applied {}W (soc window {}-{}%) mode {}".format(self.inv.id, power_w, soc_min, soc_max, mode))
        return True

    def disable_dispatch(self):
        """Stop the dispatch and clear the block.

        Idempotent: only writes when a dispatch was previously applied, or when the inverter
        reports the block still running, so the reset paths that call adjust_force_export(False)
        every cycle don't spam the block.

        Returns True only when something confirmed the block was stopped. On a failed press the
        block is still tracked and the next cycle retries - clearing `active` regardless was what
        left a running block with nothing left to stop it.
        """
        status = self._dispatch_status()
        hot = self._status_is_hot(status)
        if not self.active and not hot:
            return False
        if not self.active and status == self._disabled_status:
            # Already pressed for this exact reported state; the status sensor has not refreshed.
            return False
        disable_entity = self._entity("dispatch_disable_button")
        master_entity = self._entity("dispatch_master")
        pressed = False
        if disable_entity:
            pressed = self._press_button("dispatch disable", disable_entity)
        master_off = False
        if master_entity:
            # Always force the master off too: the disable button clears the inverter-side block
            # without updating the HA switch, leaving it optimistically "on" while the register
            # reads 0 - the exact stale-on state that makes the next apply skip the master write
            # and silently kill the dispatch. Turning it off both stops any inverter-side dispatch
            # and re-syncs the HA state so the next apply re-arms.
            master_off = self.inv.write_and_poll_switch("dispatch_master", master_entity, False, force=True)
        if not pressed and not master_off:
            self.log("Warn: Inverter {} dispatch: disable was not confirmed, retrying next cycle".format(self.inv.id))
            return False
        self.active = False
        self._disabled_status = status
        self.log("Inverter {} dispatch: disabled".format(self.inv.id))
        return True


def dispatch_power_for_rate(rate_w):
    """Convert a positive Predbat discharge rate (W) to signed dispatch power (W)."""
    return -int(rate_w)
