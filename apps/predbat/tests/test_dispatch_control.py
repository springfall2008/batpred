# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the real-time dispatch control path (dispatch.py + the execute hooks).

The dispatch feature is arg-driven: an inverter only routes export through dispatch when its
own dispatch_power / dispatch_apply_button / dispatch_control_mode resolve in apps.yaml. These
tests script the base and bare Inverter objects (no full Predbat boot) to verify:

- the power applied is the rate THIS cycle chose, not the one left on the inverter last cycle
- the dispatch SOC floor is the planned export target, not the reserve
- an inverter with no dispatch intent for the cycle has any running block stopped
- dispatch is only enabled when every required entity resolves for that inverter
- two inverters sharing one dispatch block are refused rather than left fighting over it
- a rejected staging write never reaches Apply
- disable is idempotent, and an idle status reading does not press it
- the helper (and its active flag) survives a refresh_config()
- the stock scheduled-slot path is untouched for inverters without dispatch
"""

import sys
import os
import datetime
sys.path.insert(0, os.path.dirname(__file__) + "/..")

from inverter import Inverter
from dispatch import InverterDispatch, dispatch_power_for_rate


class _DispatchBase:
    """A minimal Predbat stand-in: records service calls and serves scripted states."""

    def __init__(self, args, states, inverters=None):
        self.args = args
        self.states = states
        self.calls = []
        self.reject = set()
        self.control_ledger = None
        self.dashboard_index_app = {}
        self.minutes_now = 720
        self.plan_interval_minutes = 5
        self.midnight_utc = datetime.datetime(2026, 9, 10, 0, 0, 0)
        self.now_utc = datetime.datetime(2026, 9, 10, 12, 0, 0)  # overwritten by tests
        self.set_inverter_notify = False
        self.inverters = inverters or []

    def log(self, message):
        """Swallow the log output."""
        return None

    def record_status(self, message, had_errors=False, **kwargs):
        """Swallow the status record."""
        return None

    def get_arg(self, name, default=None, indirect=True, index=0, required_unit=None, **kwargs):
        """Serve arg values from the scripted args dict.

        Like Predbat's own get_arg, an indirect (state-backed) arg resolves to the state of the
        entity it names - that is how scheduled_discharge_enable reads "on"/"off" - while
        indirect=False returns the entity id itself, which is how the dispatch helpers resolve
        the entities they write to.
        """
        value = self.args.get(name, default)
        if isinstance(value, list):
            value = value[index] if index < len(value) else default
        if indirect and isinstance(value, str) and "." in value:
            state = self.states.get(value)
            if state is not None:
                return state
        return value

    def set_arg(self, name, value, index=0):
        """Store an arg the way Predbat's own set_arg does."""
        self.args[name] = value

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Serve the scripted state for an entity, or the default."""
        return self.states.get(entity_id, default)

    def set_state_wrapper(self, entity_id=None, state=None, attributes=None, required_unit=None):
        """Record the write."""
        self.states[entity_id] = state
        return None

    def call_service_wrapper(self, service, **kwargs):
        """Record the service call and apply its effect to the scripted states.

        An entity in self.reject accepts the call but keeps its old state, which is how a
        rejected write looks from the write-and-poll helpers: the service returns, the read
        never catches up.
        """
        entity_id = kwargs.get("entity_id")
        value = kwargs.get("value", kwargs.get("option"))
        self.calls.append(("service", service, entity_id, value))
        if entity_id in self.reject:
            return True
        if service.endswith("/set_value"):
            self.states[entity_id] = value
        elif service.endswith("/select_option"):
            self.states[entity_id] = value
        elif service.endswith("/turn_on"):
            self.states[entity_id] = "on"
        elif service.endswith("/turn_off"):
            self.states[entity_id] = "off"
        return True

    def unit_conversion(self, entity_id, state, units, required_unit, going_to=False):
        """No conversion in these tests."""
        return state


def _stub_inverter(base, inv_id=0):
    """A bare Inverter wired to a scripted base, with dispatch-relevant attributes set."""
    stub = Inverter.__new__(Inverter)
    stub.base = base
    stub.log = base.log
    stub.id = inv_id
    stub.count_register_writes = 0
    stub.registers_moved = 0
    stub.created_attributes = {}
    stub.write_backoff = {}
    stub.slept = []
    stub.sleep = lambda seconds: stub.slept.append(seconds)
    stub.inv_write_and_poll_sleep = 0
    stub.reserve_percent = 20
    stub.reserve_min = 4
    stub.track_charge_start = "00:00:00"
    stub.track_charge_end = "00:00:00"
    stub.track_discharge_start = "00:00:00"
    stub.track_discharge_end = "00:00:00"
    stub.dispatch = InverterDispatch(stub)
    return stub


def _fake_execute(stubs):
    """An Execute instance with just the state apply_dispatch_intent() reads."""
    from execute import Execute

    execute = Execute.__new__(Execute)
    execute.inverters = stubs
    execute.log = stubs[0].log
    return execute


def _services(base, service=None, entity=None):
    """Recorded service calls, optionally filtered by service and entity."""
    return [call for call in base.calls if call[0] == "service" and (service is None or call[1] == service) and (entity is None or call[2] == entity)]


DISPATCH_ARGS = {
    "dispatch_power": "number.solax_dispatch_power",
    "dispatch_apply_button": "button.solax_apply_remote_dispatch",
    "dispatch_disable_button": "button.solax_disable_remote_dispatch",
    "dispatch_control_mode": "select.solax_dispatch_control_mode",
    "dispatch_soc_min": "number.solax_dispatch_soc_min",
    "dispatch_failsafe_interval": "number.solax_dispatch_failsafe_interval",
    "dispatch_master": "switch.solax_dispatch_master",
}

STOCK_ARGS = {
    "discharge_start_time": "time.solax_discharge_start",
    "discharge_end_time": "time.solax_discharge_end",
    "scheduled_discharge_enable": "switch.solax_timed_discharge",
}

INITIAL_STATES = {
    "number.solax_dispatch_power": "0",
    "select.solax_dispatch_control_mode": "Battery Hold",
    "number.solax_dispatch_soc_min": "0",
    "number.solax_dispatch_failsafe_interval": "5",
    "switch.solax_dispatch_master": "off",
}


def test_dispatch_applies_planned_power_and_depth(my_predbat=None):
    """
    The dispatch command carries the rate and depth this cycle planned.

    The inverter is left holding a full-power 3600W block from the previous cycle while the plan
    says 500W and a 35% floor - the first-cycle-of-a-low-power-export case. The command must use
    the plan, and the master must be enabled before the power is staged (scattered writes only
    commit with the master enabled).
    """
    failed = False
    print("**** Testing dispatch applies the planned power and depth ****")

    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.states["number.solax_dispatch_power"] = "3600"
    stub = _stub_inverter(base)
    base.inverters = [stub]
    execute = _fake_execute([stub])

    execute.apply_dispatch_intent({0: 35}, {0: {"discharge_rate": 500}})

    power_writes = [call for call in _services(base, entity="number.solax_dispatch_power")]
    soc_writes = [call for call in _services(base, entity="number.solax_dispatch_soc_min")]
    apply_presses = _services(base, "button/press", "button.solax_apply_remote_dispatch")
    master_on = _services(base, "switch/turn_on", "switch.solax_dispatch_master")
    mode_writes = _services(base, "select/select_option", "select.solax_dispatch_control_mode")

    if not power_writes or power_writes[0][3] != -500:
        print("ERROR: expected dispatch power -500 (the planned rate), got {}".format(power_writes[0][3] if power_writes else None))
        failed = True
    if not soc_writes or soc_writes[0][3] != 35:
        print("ERROR: expected dispatch soc_min 35 (the planned export target), got {}".format(soc_writes[0][3] if soc_writes else None))
        failed = True
    if not mode_writes or mode_writes[0][3] != "Battery Charge":
        print("ERROR: expected dispatch mode Battery Charge, got {}".format(mode_writes[0][3] if mode_writes else None))
        failed = True
    if not apply_presses:
        print("ERROR: apply button was not pressed")
        failed = True
    if not stub.dispatch.active:
        print("ERROR: dispatch was not marked active after a successful apply")
        failed = True
    if master_on:
        master_idx = base.calls.index(("service", "switch/turn_on", "switch.solax_dispatch_master", None))
        power_idx = base.calls.index(power_writes[0])
        if master_idx > power_idx:
            print("ERROR: master turn_on must precede the staged power write")
            failed = True
    else:
        print("ERROR: master switch was not turned on")
        failed = True

    if not failed:
        print("PASS: dispatch applied -500W with a 35% floor, mode set, master on first, apply pressed")
    return 1 if failed else 0


def test_dispatch_stops_when_no_export_intent(my_predbat=None):
    """An inverter with no dispatch intent this cycle is not exporting: stop any running block."""
    failed = False
    print("**** Testing dispatch is stopped when nothing is exporting ****")

    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.states["switch.solax_dispatch_master"] = "on"
    stub = _stub_inverter(base)
    base.inverters = [stub]
    stub.dispatch.active = True
    execute = _fake_execute([stub])

    execute.apply_dispatch_intent({}, {0: {"discharge_rate": None}})

    disable_presses = _services(base, "button/press", "button.solax_disable_remote_dispatch")
    master_off = _services(base, "switch/turn_off", "switch.solax_dispatch_master")
    if len(disable_presses) != 1:
        print("ERROR: expected exactly 1 disable press, got {}".format(len(disable_presses)))
        failed = True
    if len(master_off) != 1:
        print("ERROR: expected exactly 1 master turn_off, got {}".format(len(master_off)))
        failed = True
    if stub.dispatch.active:
        print("ERROR: dispatch is still marked active after being stopped")
        failed = True

    if not failed:
        print("PASS: a cycle with no export stops the dispatch block")
    return 1 if failed else 0


def test_dispatch_enabled_requires_per_inverter_entities(my_predbat=None):
    """
    A per-inverter arg list that is short must leave the uncovered inverter on the slot path.

    With a one-item list and two inverters, inverter 1 resolves no dispatch entity. Reporting
    dispatch as enabled on arg presence alone left it skipping the scheduled-slot path that it
    could actually have used, so it never exported at all.
    """
    failed = False
    print("**** Testing dispatch needs its entities on each inverter ****")

    args = {
        "dispatch_power": ["number.solax_dispatch_power"],
        "dispatch_apply_button": ["button.solax_apply_remote_dispatch"],
        "dispatch_control_mode": ["select.solax_dispatch_control_mode"],
    }
    base = _DispatchBase(args, dict(INITIAL_STATES))
    stub0 = _stub_inverter(base, 0)
    stub1 = _stub_inverter(base, 1)
    base.inverters = [stub0, stub1]

    if not stub0.dispatch.enabled():
        print("ERROR: inverter 0 should be dispatch-enabled (its entities resolve)")
        failed = True
    if stub1.dispatch.enabled():
        print("ERROR: inverter 1 has no dispatch entities but reports dispatch enabled")
        failed = True

    if not failed:
        print("PASS: only the inverter with resolvable dispatch entities is enabled")
    return 1 if failed else 0


def test_dispatch_refuses_a_shared_block(my_predbat=None):
    """
    Two inverters cannot drive one dispatch block: each would overwrite the other's power, so a
    single-value apps.yaml arg is refused for both rather than half-working.
    """
    failed = False
    print("**** Testing dispatch refuses a dispatch block shared by two inverters ****")

    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    stub0 = _stub_inverter(base, 0)
    stub1 = _stub_inverter(base, 1)
    base.inverters = [stub0, stub1]

    if stub0.dispatch.enabled() or stub1.dispatch.enabled():
        print("ERROR: dispatch reported enabled for a block shared by two inverters")
        failed = True

    if not failed:
        print("PASS: a shared dispatch block is refused for both inverters")
    return 1 if failed else 0


def test_dispatch_failed_power_write_blocks_apply(my_predbat=None):
    """
    A staging write the inverter rejects must not reach Apply.

    The old path pressed Apply regardless, committing whatever had been staged before and
    logging the new power as applied.
    """
    failed = False
    print("**** Testing a rejected power write never reaches Apply ****")

    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.reject = {"number.solax_dispatch_power"}
    stub = _stub_inverter(base)
    base.inverters = [stub]

    applied = stub.dispatch.apply_dispatch(power_w=-500, soc_min=35, soc_max=100)

    apply_presses = _services(base, "button/press", "button.solax_apply_remote_dispatch")
    if applied:
        print("ERROR: apply_dispatch reported success after the power write was rejected")
        failed = True
    if apply_presses:
        print("ERROR: Apply was pressed after the power write failed")
        failed = True
    if stub.dispatch.active:
        print("ERROR: dispatch is marked active after a failed staging write")
        failed = True

    if not failed:
        print("PASS: a rejected power write stops the dispatch before Apply")
    return 1 if failed else 0


def test_dispatch_disable_ignores_idle_status(my_predbat=None):
    """An idle status reading ("Disabled", "Stopped", "Demand") must not press Disable every cycle."""
    failed = False
    print("**** Testing idle dispatch status does not press Disable ****")

    for status in ("Disabled", "Stopped", "Demand", "Idle", "0", "unavailable"):
        base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
        base.args["dispatch_status"] = "sensor.solax_dispatch_status"
        base.states["sensor.solax_dispatch_status"] = status
        stub = _stub_inverter(base)
        base.inverters = [stub]

        stub.dispatch.disable_dispatch()
        if _services(base, "button/press"):
            print("ERROR: status {!r} was treated as active and pressed Disable".format(status))
            failed = True

    if not failed:
        print("PASS: idle status readings do not press Disable")
    return 1 if failed else 0


def test_dispatch_disable_is_idempotent(my_predbat=None):
    """Disable presses once for an active dispatch, and not again for the same still-fresh status."""
    failed = False
    print("**** Testing dispatch disable is idempotent ****")

    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.args["dispatch_status"] = "sensor.solax_dispatch_status"
    base.states["sensor.solax_dispatch_status"] = "1"
    base.states["switch.solax_dispatch_master"] = "on"
    stub = _stub_inverter(base)
    base.inverters = [stub]
    stub.dispatch.active = True

    stub.dispatch.disable_dispatch()
    stub.dispatch.disable_dispatch()
    disable_presses = _services(base, "button/press", "button.solax_disable_remote_dispatch")
    if len(disable_presses) != 1:
        print("ERROR: expected exactly 1 disable press across two calls, got {}".format(len(disable_presses)))
        failed = True
    master_off = _services(base, "switch/turn_off", "switch.solax_dispatch_master")
    if len(master_off) != 1:
        print("ERROR: expected exactly 1 master turn_off, got {}".format(len(master_off)))
        failed = True

    # A cold start with nothing reported must not press at all
    base2 = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    stub2 = _stub_inverter(base2)
    base2.inverters = [stub2]
    stub2.dispatch.disable_dispatch()
    if _services(base2, "button/press"):
        print("ERROR: disable pressed from cold with nothing active")
        failed = True

    if not failed:
        print("PASS: disable presses once, and not at all when nothing is active")
    return 1 if failed else 0


def test_dispatch_state_survives_refresh_config(my_predbat):
    """
    The helper - and its active flag - must survive a cycle boundary.

    refresh_config() runs every plan cycle; when it rebuilt the helper it reset `active` to
    False, so at the end of the window disable_dispatch() returned early and the battery kept
    exporting until the inverter's failsafe expired.
    """
    failed = False
    print("**** Testing dispatch state survives refresh_config ****")

    if my_predbat is None:
        print("   (no Predbat fixture, skipped)")
        return 0

    inverter = Inverter(my_predbat, id=0)
    helper = inverter.dispatch
    helper.active = True
    inverter.refresh_config()
    inverter.refresh_config(quiet=True)

    if inverter.dispatch is not helper:
        print("ERROR: refresh_config() rebuilt the dispatch helper")
        failed = True
    if not inverter.dispatch.active:
        print("ERROR: refresh_config() reset the dispatch active flag")
        failed = True

    if not failed:
        print("PASS: the dispatch helper and its active flag survive refresh_config()")
    return 1 if failed else 0


def test_dispatch_unconfigured_uses_stock_path(my_predbat=None):
    """
    Without dispatch args, adjust_force_export must behave exactly as before - the
    scheduled-slot path runs and nothing dispatch-related is touched. Regression
    guard for existing users.
    """
    failed = False
    print("**** Testing unconfigured inverter keeps the stock scheduled-slot path ****")

    start = datetime.datetime(2026, 9, 10, 1, 0, 0)
    end = datetime.datetime(2026, 9, 10, 2, 0, 0)
    base = _DispatchBase(dict(STOCK_ARGS), {"switch.solax_timed_discharge": "off"})
    base.now_utc = start + datetime.timedelta(minutes=30)
    stub = _stub_inverter(base)
    base.inverters = [stub]
    stub.inv_has_discharge_enable_time = True
    stub.inv_has_ge_inverter_mode = False
    stub.inv_has_idle_time = False
    stub.inv_time_button_press = False
    stub.inv_charge_time_format = "H M"
    stub.discharge_start_time_minutes = 0
    stub.charge_start_time_minutes = 0
    stub.charge_end_time_minutes = 0
    stub.base.inverter_clock_skew_discharge_start = 0
    stub.base.inverter_clock_skew_discharge_end = 0
    stub.base.set_inverter_notify = False
    stub.base.minutes_now = 90
    stub.base.midnight_utc = datetime.datetime(2026, 9, 10, 0, 0, 0)
    # Idle-time bookkeeping is out of scope for this test; the slot writes are the point.
    stub.adjust_idle_time = lambda **kwargs: None

    try:
        stub.adjust_force_export(True, start, end)
    except Exception as e:
        # The stock path needs more scripted state than we provide; what matters is
        # that it ATTEMPTED the slot writes before any failure
        print("   (stock path raised {} - treated as reaching the slot logic)".format(type(e).__name__))
    any_slot_write = any(call[2] in (STOCK_ARGS["discharge_start_time"], STOCK_ARGS["discharge_end_time"], STOCK_ARGS["scheduled_discharge_enable"]) for call in _services(base))
    if not any_slot_write:
        print("ERROR: stock path did not write any slot entity (and no exception path was exercised)")
        failed = True
    if _services(base, "button/press"):
        print("ERROR: stock path pressed a dispatch button")
        failed = True

    if not failed:
        print("PASS: stock path still writes slot entities and never touches dispatch")
    return 1 if failed else 0


def test_dispatch_disarms_a_stale_slot(my_predbat=None):
    """
    A timed export slot armed before dispatch was configured must be turned off.

    The dispatch branch returns before the stock slot writes, so without this the slot kept
    exporting alongside the dispatch block, with neither mechanism aware of the other.
    """
    failed = False
    print("**** Testing dispatch disarms a stale scheduled export slot ****")

    args = dict(DISPATCH_ARGS)
    args.update({"scheduled_discharge_enable": "switch.solax_timed_discharge"})
    base = _DispatchBase(args, dict(INITIAL_STATES))
    base.states["switch.solax_timed_discharge"] = "on"
    stub = _stub_inverter(base)
    base.inverters = [stub]
    stub.adjust_idle_time = lambda **kwargs: None

    stub.adjust_force_export(False)

    slot_off = _services(base, "switch/turn_off", "switch.solax_timed_discharge")
    if not slot_off:
        print("ERROR: the armed scheduled export slot was left on")
        failed = True
    if any(call[2] in (STOCK_ARGS["discharge_start_time"], STOCK_ARGS["discharge_end_time"]) for call in _services(base)):
        print("ERROR: dispatch path wrote the slot times")
        failed = True

    if not failed:
        print("PASS: a stale scheduled export slot is disarmed while dispatch is in charge")
    return 1 if failed else 0


def test_dispatch_power_for_rate_sign_convention(my_predbat=None):
    """
    The dispatch protocol wants signed power (negative = discharge). Predbat computes
    export power as a positive rate in W. dispatch_power_for_rate flips the sign.
    """
    failed = False
    print("**** Testing the dispatch power sign convention ****")
    if dispatch_power_for_rate(3600) != -3600:
        print("ERROR: dispatch_power_for_rate(3600) != -3600")
        failed = True
    if dispatch_power_for_rate(0) != 0:
        print("ERROR: dispatch_power_for_rate(0) != 0")
        failed = True
    if not failed:
        print("PASS: positive discharge rate maps to negative dispatch power")
    return 1 if failed else 0


def run_dispatch_control_tests(my_predbat):
    """Run every real-time dispatch control test, returning a non-zero count on failure."""
    failed = 0
    failed += test_dispatch_applies_planned_power_and_depth(my_predbat)
    failed += test_dispatch_stops_when_no_export_intent(my_predbat)
    failed += test_dispatch_enabled_requires_per_inverter_entities(my_predbat)
    failed += test_dispatch_refuses_a_shared_block(my_predbat)
    failed += test_dispatch_failed_power_write_blocks_apply(my_predbat)
    failed += test_dispatch_disable_ignores_idle_status(my_predbat)
    failed += test_dispatch_disable_is_idempotent(my_predbat)
    failed += test_dispatch_state_survives_refresh_config(my_predbat)
    failed += test_dispatch_disarms_a_stale_slot(my_predbat)
    failed += test_dispatch_unconfigured_uses_stock_path(my_predbat)
    failed += test_dispatch_power_for_rate_sign_convention(my_predbat)
    return failed
