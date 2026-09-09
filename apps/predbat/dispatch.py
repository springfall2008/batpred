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

Optional apps.yaml args (all-or-nothing; absence leaves the scheduled-slot path
untouched, so existing configs are unaffected):

    dispatch_apply_button:      button that block-writes the staged dispatch block
    dispatch_disable_button:    button that stops dispatch and clears the block
    dispatch_control_mode:      select holding the dispatch mode (e.g. "Battery Charge")
    dispatch_power:             number holding the dispatch power in W, signed
                                (negative = discharge / export on Solis)
    dispatch_soc_min:           number holding the dispatch SOC floor %
    dispatch_soc_max:           number holding the dispatch SOC ceiling % (optional)
    dispatch_failsafe_interval: number holding the failsafe window in minutes (optional)
    dispatch_master:            switch enabling the dispatch block (optional; some
                                integrations gate the block on a master enable)

Convention: discharge/export commands are negative power. Predbat computes export
power as a positive discharge rate in W, so the sign is flipped on write.
"""

class InverterDispatch:
    """Per-inverter helper that stages and applies real-time dispatch commands."""

    def __init__(self, inverter):
        """Bind to the parent inverter and resolve the dispatch entities from apps.yaml."""
        self.inv = inverter
        self.base = inverter.base
        self.log = inverter.log
        # Track the last power we applied so a mid-window change re-applies the block
        self.last_power_w = None
        self.last_mode = None
        self.active = False

    def enabled(self):
        """True when the inverter is dispatch-capable and the entity args are configured.

        Pure arg-presence gate (mirrors the optional discharge_target_soc pattern), so
        a user with the integration entities but no apps.yaml wiring keeps the stock
        scheduled-slot export behaviour untouched.
        """
        return "dispatch_power" in self.base.args and "dispatch_apply_button" in self.base.args

    def _entity(self, arg):
        """Return the entity_id for an arg, or None."""
        entity_id = self.base.get_arg(arg, indirect=False, index=self.inv.id)
        return entity_id if entity_id else None

    def _write_and_poll(self, name, entity_id, value, fuzzy=0, required_unit=None):
        """Route a write through the inverter's write_and_poll helpers (ledger-aware)."""
        return self.inv.write_and_poll_value(name, entity_id, value, fuzzy=fuzzy, required_unit=required_unit)

    def _press_button(self, name, entity_id):
        """Press a button entity via HA services, mirroring _press_single_toggle_button."""
        self.base.call_service_wrapper("button/press", entity_id=entity_id)
        self.log("Inverter {} dispatch: pressed {} ({})".format(self.inv.id, name, entity_id))
        return True

    def apply_dispatch(self, power_w, soc_min=None, soc_max=None, mode="Battery Charge"):
        """Stage the dispatch parameters and apply the dispatch block.

        Stages mode/power/SOC bounds into the config entities, enables the master
        (when present), then presses the apply button. Safe to call every Predbat
        cycle while an export window is active: write_and_poll skips no-op writes,
        but the apply button is re-pressed each call to feed the failsafe.

        Args:
            power_w: signed dispatch power in W (positive = charge, negative = discharge)
            soc_min: optional SOC floor % for the dispatch (None = leave staged)
            soc_max: optional SOC ceiling % for the dispatch (None = leave staged)
            mode:    dispatch mode name (default "Battery Charge" = battery charge/
                     discharge by power sign, on the Solis protocol)
        """
        mode_entity = self._entity("dispatch_control_mode")
        power_entity = self._entity("dispatch_power")
        if not power_entity:
            self.log("Warn: Inverter {} dispatch: no dispatch_power entity configured, dispatch not applied".format(self.inv.id))
            return False

        # Stage the mode first (it selects which element the dispatch acts on)
        if mode_entity and mode != self.last_mode:
            self.inv.write_and_poll_option("dispatch_control_mode", mode_entity, mode)
            self.last_mode = mode

        # Stage the signed power (discharge negative)
        self._write_and_poll("dispatch_power", power_entity, int(power_w), required_unit="W")

        # Stage the SOC window
        if soc_min is not None:
            soc_min_entity = self._entity("dispatch_soc_min")
            if soc_min_entity:
                self._write_and_poll("dispatch_soc_min", soc_min_entity, int(soc_min), required_unit="%")
        if soc_max is not None:
            soc_max_entity = self._entity("dispatch_soc_max")
            if soc_max_entity:
                self._write_and_poll("dispatch_soc_max", soc_max_entity, int(soc_max), required_unit="%")
        # Failsafe interval: once at first apply, keep staged thereafter (cheap no-op
        # writes skipped by write_and_poll)
        failsafe_entity = self._entity("dispatch_failsafe_interval")
        if failsafe_entity and not self.active:
            self._write_and_poll("dispatch_failsafe_interval", failsafe_entity, 10, required_unit="min")

        # Master enable (present on Solis; integrations without one skip this)
        master_entity = self._entity("dispatch_master")
        if master_entity:
            self.inv.write_and_poll_switch("dispatch_master", master_entity, True)

        # Apply the block
        apply_entity = self._entity("dispatch_apply_button")
        if not apply_entity:
            self.log("Warn: Inverter {} dispatch: no apply button configured, dispatch not applied".format(self.inv.id))
            return False
        self._press_button("dispatch apply", apply_entity)
        self.active = True
        self.last_power_w = power_w
        self.log("Inverter {} dispatch: applied {}W (soc window {}-{}%)".format(self.inv.id, power_w, soc_min, soc_max))
        return True

    def disable_dispatch(self):
        """Stop the dispatch and clear the block.

        Idempotent: only writes when a dispatch was previously applied, so the reset
        paths that call adjust_force_export(False) every cycle don't spam the block.
        """
        disable_entity = self._entity("dispatch_disable_button")
        master_entity = self._entity("dispatch_master")
        if not self.active and not self._block_is_hot():
            return False
        if disable_entity:
            self._press_button("dispatch disable", disable_entity)
        elif master_entity:
            self.inv.write_and_poll_switch("dispatch_master", master_entity, False)
        self.active = False
        self.last_power_w = None
        self.log("Inverter {} dispatch: disabled".format(self.inv.id))
        return True

    def _block_is_hot(self):
        """Return True when the inverter-side dispatch block reports active (status sensor), if published."""
        status_entity = self._entity("dispatch_status")
        if status_entity:
            try:
                state = self.base.get_state_wrapper(status_entity)
                return state is not None and str(state).lower() not in ("idle", "0", "off", "unknown", "unavailable")
            except Exception:
                return False
        return False

    def adjust_dispatch_for_export(self, discharge_rate_w):
        """Convert a Predbat discharge rate into a dispatch command.

        Export windows call this with the positive discharge rate in W. The dispatch
        protocol wants signed power (negative = discharge), so the sign is flipped
        here - the single place where the convention lives.
        """
        return -int(discharge_rate_w)


def dispatch_power_for_rate(rate_w):
    """Convert a positive Predbat discharge rate (W) to signed dispatch power (W)."""
    return -int(rate_w)