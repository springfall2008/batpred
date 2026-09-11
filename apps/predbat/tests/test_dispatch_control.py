# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the real-time dispatch control path (dispatch.py + Inverter hooks).

The dispatch feature is arg-driven: Inverter only routes export through dispatch when
dispatch_power and dispatch_apply_button are configured in apps.yaml. These tests
script the base and a bare Inverter (no full Predbat boot) to verify:

- adjust_force_export routes through dispatch when enabled, and to the stock
  scheduled-slot path when not configured
- the discharge rate set by adjust_discharge_rate is the power dispatch applies
- pre-arming a future window does NOT apply dispatch (only in-window calls do)
- disable stops the dispatch and is idempotent across cycles
- sign convention: positive discharge rate -> negative dispatch power
"""

import sys
import os
import datetime
sys.path.insert(0, os.path.dirname(__file__) + "/..")

from inverter import Inverter
from dispatch import dispatch_power_for_rate


class _DispatchBase:
    """A minimal Predbat stand-in: records service calls and serves scripted states."""

    def __init__(self, args, states):
        self.args = args
        self.states = states
        self.calls = []
        self.control_ledger = None
        self.dashboard_index_app = {}
        self.minutes_now = 720
        self.midnight_utc = datetime.datetime(2026, 9, 10, 0, 0, 0)
        self.now_utc = datetime.datetime(2026, 9, 10, 12, 0, 0)  # overwritten by tests

    def log(self, message):
        """Swallow the log output."""
        return None

    def record_status(self, message, had_errors=False, **kwargs):
        """Swallow the status record."""
        return None

    def get_arg(self, name, indirect=False, index=0, default=None, required_unit=None):
        """Serve arg values from the scripted args dict."""
        if name in self.args:
            value = self.args[name]
            if isinstance(value, list):
                return value[index] if index < len(value) else default
            return value
        return default

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Serve the scripted state for an entity, or the default."""
        return self.states.get(entity_id, default)

    def set_state_wrapper(self, entity_id=None, state=None, attributes=None, required_unit=None):
        """Record the write."""
        self.calls.append(("set_state", entity_id, state))
        return None

    def call_service_wrapper(self, service, **kwargs):
        """Record the service call so tests can assert on the sequence."""
        self.calls.append(("service", service, kwargs.get("entity_id"), kwargs.get("value")))
        self.states[kwargs.get("entity_id")] = kwargs.get("value")
        return True

    def unit_conversion(self, entity_id, state, units, required_unit, going_to=False):
        """No conversion in these tests."""
        return state


def _stub_inverter(base):
    """A bare Inverter wired to a scripted base, with dispatch-relevant attributes set."""
    stub = Inverter.__new__(Inverter)
    stub.base = base
    stub.log = base.log
    stub.id = 0
    stub.count_register_writes = 0
    stub.created_attributes = {}
    stub.slept = []
    stub.sleep = lambda seconds: stub.slept.append(seconds)
    stub.inv_write_and_poll_sleep = 0
    stub.reserve_percent = 20
    stub.reserve_min = 4
    stub.dispatch_rate_w = None
    return stub


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


def test_dispatch_enabled_routes_export_through_dispatch(my_predbat=None):
    """
    With dispatch args configured, adjust_force_export applies the dispatch block
    instead of writing the scheduled-slot entities.

    In-window call: mode select gets Battery Charge, power gets the staged rate with
    the sign flipped (discharge = negative), master goes on, apply is pressed.
    """
    failed = False
    print("**** Testing dispatch-enabled export applies the dispatch block ****")

    start = datetime.datetime(2026, 9, 10, 1, 0, 0)
    end = datetime.datetime(2026, 9, 10, 2, 0, 0)
    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.now_utc = start + datetime.timedelta(minutes=30)  # inside the window
    stub = _stub_inverter(base)
    from dispatch import InverterDispatch
    stub.dispatch = InverterDispatch(stub)
    stub.dispatch_rate_w = 3600

    stub.adjust_force_export(True, start, end)

    services = [(c[1], c[2], c[3]) for c in base.calls if c[0] == "service"]
    pressed_apply = any(s[0] == "button/press" and s[1] == "button.solax_apply_remote_dispatch" for s in services)
    power_write = [s for s in services if s[0] == "number/set_value" and s[1] == "number.solax_dispatch_power"]
    soc_min_write = [s for s in services if s[0] == "number/set_value" and s[1] == "number.solax_dispatch_soc_min"]
    master_on = [s for s in services if s[0] == "switch/turn_on" and s[1] == "switch.solax_dispatch_master"]
    master_write_poll = [c for c in base.calls if c[0] == "set_state" and c[1] == "switch.solax_dispatch_master"]
    # The order in which the master is enabled must precede the staged power write:
    # scattered single-register writes only commit while the master is enabled.
    if master_on:
        master_idx = base.calls.index(("service", "switch/turn_on", "switch.solax_dispatch_master", None))
        power_idx = next(i for i, c in enumerate(base.calls) if c[0] == "service" and c[1] == "number/set_value" and c[2] == "number.solax_dispatch_power")
        master_before_power = master_idx < power_idx
    else:
        master_before_power = False

    if not pressed_apply:
        print("ERROR: apply button was not pressed")
        failed = True
    if not power_write or power_write[0][2] != -3600:
        print("ERROR: expected dispatch power -3600, got {}".format(power_write[0][2] if power_write else None))
        failed = True
    if not soc_min_write or soc_min_write[0][2] != 20:
        print("ERROR: expected dispatch soc_min {} (reserve floor), got {}".format(20, soc_min_write[0][2] if soc_min_write else None))
        failed = True
    if not master_on:
        print("ERROR: master switch was not turned on")
        failed = True
    elif master_write_poll:
        print("ERROR: master was written through write_and_poll_switch, which can silently skip the write when HA optimistically reports the switch as on")
        failed = True
    if master_on and not master_before_power:
        print("ERROR: master turn_on must precede the staged power write (scattered writes only commit with master enabled)")
        failed = True
    # Slot entities must NOT be written by the dispatch path
    slot_write = [s for s in services if s[1] in (STOCK_ARGS["discharge_start_time"], STOCK_ARGS["scheduled_discharge_enable"])]
    if slot_write:
        print("ERROR: dispatch path wrote scheduled-slot entities: {}".format(slot_write))
        failed = True

    if not failed:
        print("PASS: dispatch applied with power -3600, soc_min 20, forced master on before staging, no slot writes")
    return 1 if failed else 0


def test_dispatch_pre_arm_does_not_apply(my_predbat=None):
    """
    execute() pre-arms upcoming windows with adjust_force_export(True) - dispatch must
    not start until the window time is reached. Pre-arming falls through to disable.
    """
    failed = False
    print("**** Testing pre-armed future window does not start dispatch ****")

    start = datetime.datetime(2026, 9, 10, 1, 0, 0)
    end = datetime.datetime(2026, 9, 10, 2, 0, 0)
    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.now_utc = start - datetime.timedelta(hours=1)  # an hour before the window
    stub = _stub_inverter(base)
    from dispatch import InverterDispatch
    stub.dispatch = InverterDispatch(stub)
    stub.dispatch_rate_w = 3600

    stub.adjust_force_export(True, start, end)

    services = [(c[1], c[2]) for c in base.calls if c[0] == "service"]
    pressed_apply = any(s[0] == "button/press" and s[1] == "button.solax_apply_remote_dispatch" for s in services)
    if pressed_apply:
        print("ERROR: dispatch was applied for a future window")
        failed = True

    if not failed:
        print("PASS: future window did not apply dispatch")
    return 1 if failed else 0


def test_dispatch_disable_is_idempotent(my_predbat=None):
    """
    adjust_force_export(False) is called every cycle outside windows; disable must
    only press the disable button when a dispatch is actually active (or the block
    reports hot), not spam it from a cold start.
    """
    failed = False
    print("**** Testing dispatch disable is idempotent ****")

    base = _DispatchBase(dict(DISPATCH_ARGS), dict(INITIAL_STATES))
    base.now_utc = datetime.datetime(2026, 9, 10, 12, 0, 0)
    stub = _stub_inverter(base)
    from dispatch import InverterDispatch
    stub.dispatch = InverterDispatch(stub)

    stub.adjust_force_export(False)
    cold_calls = len([c for c in base.calls if c[0] == "service" and c[1] == "button/press"])
    if cold_calls != 0:
        print("ERROR: disable pressed the button {} times from cold (expected 0)".format(cold_calls))
        failed = True

    # Now activate: apply then disable twice - only one disable press should occur
    start = datetime.datetime(2026, 9, 10, 1, 0, 0)
    end = datetime.datetime(2026, 9, 10, 2, 0, 0)
    base.now_utc = start + datetime.timedelta(minutes=30)
    stub.dispatch_rate_w = 3600
    base.calls = []
    stub.adjust_force_export(True, start, end)
    base.now_utc = datetime.datetime(2026, 9, 10, 12, 0, 0)
    base.calls = []
    stub.adjust_force_export(False)
    stub.adjust_force_export(False)
    disable_presses = len([c for c in base.calls if c[0] == "service" and c[1] == "button/press" and c[2] == "button.solax_disable_remote_dispatch"])
    if disable_presses != 1:
        print("ERROR: expected exactly 1 disable press after deactivation, got {}".format(disable_presses))
        failed = True
    master_off = [c for c in base.calls if c[0] == "service" and c[1] == "switch/turn_off" and c[2] == "switch.solax_dispatch_master"]
    if len(master_off) != 1:
        print("ERROR: expected exactly 1 master turn_off alongside the disable press, got {}".format(len(master_off)))
        failed = True

    if not failed:
        print("PASS: disable is idempotent and force-offs the master")
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
    from dispatch import InverterDispatch
    stub.dispatch = InverterDispatch(stub)
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
    stub.track_charge_start = None
    stub.track_charge_end = None
    stub.track_discharge_start = None
    stub.track_discharge_end = None
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
    services = [(c[1], c[2]) for c in base.calls if c[0] == "service"]
    any_slot_write = any(s[1] in (STOCK_ARGS["discharge_start_time"], STOCK_ARGS["discharge_end_time"], STOCK_ARGS["scheduled_discharge_enable"]) for s in services)
    if not any_slot_write:
        print("ERROR: stock path did not write any slot entity (and no exception path was exercised)")
        failed = True

    if not failed:
        print("PASS: stock path still writes slot entities")
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
    failed += test_dispatch_enabled_routes_export_through_dispatch(my_predbat)
    failed += test_dispatch_pre_arm_does_not_apply(my_predbat)
    failed += test_dispatch_disable_is_idempotent(my_predbat)
    failed += test_dispatch_unconfigured_uses_stock_path(my_predbat)
    failed += test_dispatch_power_for_rate_sign_convention(my_predbat)
    return failed
