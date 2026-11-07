# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the shared Predbat-led EV charger control mixin."""

import datetime
import os
import sys

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_infra import run_async
from car_charger_control import CarChargerControl, ChargerControlDeferred, GUEST_CHARGING_MAX_HOURS, OCTOPUS_DISCOVERY_WAIT_MINUTES, parse_control_setting, parse_dispatch_time

LONDON = pytz.timezone("Europe/London")


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self, saved=None, fail=False):
        """Start with an optional saved record, or fail every call."""
        self.saved = dict(saved or {})
        self.fail = fail
        self.saves = 0

    async def save(self, module, key, value):
        """Record a save."""
        self.saves += 1
        if self.fail:
            raise OSError("disk full")
        self.saved[(module, key)] = value

    async def load(self, module, key):
        """Return what was saved."""
        if self.fail:
            raise OSError("disk gone")
        return self.saved.get((module, key))


class FakeBase:
    """The parts of the base object the mixin reads."""

    def __init__(self):
        """Not read only unless a test says so."""
        self.set_read_only = False
        self.set_read_only_axle = False
        self.num_cars = 1


class FakeCharger:
    """A charger that can tell whether a car is connected and what state it is in."""

    def __init__(self, key, connected=True):
        """A charger that is currently off."""
        self.key = key
        self.connected = connected
        self.charging = False
        # Set by a test for a charger that reports its car unplugged
        self.unplugged = False


class FakeComponent(CarChargerControl):
    """A minimal charger component using the mixin, recording every command."""

    def __init__(self, chargers, storage=None, drift_aware=False):
        """Wire up a component with the given chargers."""
        self.base = FakeBase()
        self.prefix = "predbat"
        self.local_tz = LONDON
        self.storage = storage
        self.chargers = chargers
        self.drift_aware = drift_aware
        self.args = {}
        self.plans = {}
        # entity id -> attributes of the Intelligent dispatch sensors
        self.sensors = {}
        self.logs = []
        self.commands = []
        # Chargers whose commands are refused
        self.refusing = set()
        self.charger_control_setup("Fake", "charger", "fake", "control_state", "enabled")
        self.charger_control_active = True

    @property
    def num_cars(self):
        """Mirror ComponentBase.num_cars."""
        return self.base.num_cars

    def log(self, message):
        """Record a log line."""
        self.logs.append(message)

    def get_arg(self, name, default=None, **kwargs):
        """Config args, as ComponentBase.get_arg."""
        return self.args.get(name, default)

    def get_state_wrapper(self, entity_id, default=None, attribute=None):
        """The car charging slot plans, or an attribute of a dispatch sensor."""
        if attribute == "planned":
            return self.plans.get(entity_id, default)
        return self.sensors.get(entity_id, {}).get(attribute or "state", default)

    def charger_control_chargers(self):
        """Chargers in car order."""
        return [(charger.key, charger) for charger in self.chargers]

    def charger_control_connected(self, handle):
        """Use the fake's plug state."""
        return handle.connected

    def charger_control_unplugged(self, handle):
        """The fake's own unplug report."""
        return handle.unplugged

    def charger_control_drifted(self, handle, charge):
        """Only when the test asks for a drift-aware charger."""
        return self.drift_aware and handle.charging != charge

    async def charger_control_send(self, handle, charge, car_n):
        """Record and apply the command, unless this charger refuses it."""
        if handle.key in self.refusing:
            raise RuntimeError("charger {} refused".format(handle.key))
        self.commands.append((handle.key, "on" if charge else "off", car_n))
        handle.charging = charge

    async def charger_control_release_one(self, handle, charge):
        """Record the release, unless this charger refuses it."""
        if handle.key in self.refusing:
            raise RuntimeError("charger {} refused".format(handle.key))
        self.commands.append((handle.key, "release", charge))


def _now():
    """A fixed instant: 1 Jun 2026 01:30 London time."""
    return LONDON.localize(datetime.datetime(2026, 6, 1, 1, 30))


def _plan(component, car_n, windows):
    """Publish a plan for one car as output.py would."""
    postfix = "" if car_n == 0 else "_{}".format(car_n)
    component.plans["binary_sensor.predbat_car_charging_slot" + postfix] = [{"start": start, "end": end} for start, end in windows]


def test_no_plan_sends_nothing():
    """Before any plan is published nothing is commanded - a restart must not stop a charge."""
    component = FakeComponent([FakeCharger("a")])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


def test_on_inside_window_off_outside():
    """Charges inside a planned window and stops outside one, sending only on a change."""
    component = FakeComponent([FakeCharger("a")])
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0)], component.commands
    run_async(component.charger_control_tick(_now()))
    assert len(component.commands) == 1, "An unchanged state must not be re-sent"
    run_async(component.charger_control_tick(_now() + datetime.timedelta(hours=1)))
    assert component.commands[-1] == ("a", "off", 0), component.commands


def test_empty_plan_stops():
    """A published but empty plan means hold the charger off."""
    component = FakeComponent([FakeCharger("a")])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


def test_each_charger_follows_its_own_car():
    """Charger N follows car N's plan."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    _plan(component, 0, [])
    _plan(component, 1, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("b", "on", 1)], component.commands


def test_car_whose_plan_is_not_published_yet_is_left_alone():
    """Car 1's plan not being published yet is unknown, not empty - its charger must not be
    stopped just because car 0's plan has appeared first."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    assert "b" not in component.charger_control_state, component.charger_control_state

    _plan(component, 1, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("b", "off", 1)], component.commands


def test_charger_without_a_car_is_left_alone():
    """A charger beyond num_cars has no plan of its own and must not be stopped."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


def test_charger_whose_car_goes_is_released():
    """A held charger that falls beyond num_cars is handed back, not left with nobody in control."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    _plan(component, 0, [])
    _plan(component, 1, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("b", "on", 1)], component.commands

    component.base.num_cars = 1
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands[2:] == [("b", "release", True)], component.commands
    assert component.charger_control_state == {"a": False}, component.charger_control_state


def test_disconnected_charger_is_left_alone():
    """No car on the cable - nothing to command."""
    component = FakeComponent([FakeCharger("a", connected=False)])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


def test_drift_is_reapplied():
    """A charger changed behind Predbat's back is set again."""
    charger = FakeCharger("a")
    component = FakeComponent([charger], drift_aware=True)
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    charger.charging = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("a", "on", 0)], component.commands
    assert any("re-applying" in line for line in component.logs), component.logs


def test_read_only_releases_once_and_resumes():
    """Read only hands back only chargers Predbat moved, once, then control resumes when it clears."""
    # b has no car to follow, so Predbat never moves it and must not release it either
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    component.args["set_read_only"] = True
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], component.commands
    assert component.charger_control_state == {}, "Released chargers are forgotten"
    component.args["set_read_only"] = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0), component.commands
    assert any("Read only mode cleared" in line for line in component.logs), component.logs


def test_read_only_comes_from_the_setting_and_axle():
    """Read only is the setting, or Axle's VPP flag - not the base attribute, which reset() leaves True until the first update_pred()."""
    component = FakeComponent([FakeCharger("a")])
    component.base.set_read_only = True
    assert component.charger_control_read_only_now() is False, "a restart must not read as read only"
    component.args["set_read_only"] = True
    assert component.charger_control_read_only_now() is True
    component.args["set_read_only"] = False
    component.base.set_read_only_axle = True
    assert component.charger_control_read_only_now() is True


def test_switch_off_releases_and_persists():
    """Turning the switch off releases, and is saved so a restart keeps it off."""
    storage = FakeStorage()
    component = FakeComponent([FakeCharger("a")], storage=storage)
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_set_enabled(False))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("a", "release", True)], component.commands
    assert storage.saved[("fake", "control_state")] == {"enabled": False, "held": {}}

    restarted = FakeComponent([FakeCharger("a")], storage=storage)
    run_async(restarted.charger_control_load_enabled())
    assert restarted.charger_control_enabled is False, "The off must survive a restart"


def test_storage_failures_fail_soft():
    """A broken store logs a warning and leaves the switch on."""
    component = FakeComponent([FakeCharger("a")], storage=FakeStorage(fail=True))
    run_async(component.charger_control_load_enabled())
    run_async(component.charger_control_save_enabled())
    assert component.charger_control_enabled is True
    assert sum("Warn" in line for line in component.logs) == 2, component.logs


def test_storage_save_returning_false_is_a_failed_save():
    """Storage reports serialisation and I/O failures by returning False, which must be warned about; None is a pass."""
    component = FakeComponent([FakeCharger("a")], storage=FakeStorage())

    async def refused(module, key, value):
        """A save that Storage refuses without raising."""
        return False

    component.storage.save = refused
    run_async(component.charger_control_save_enabled())
    assert sum("Warn" in line for line in component.logs) == 1, component.logs

    for ok in (None, True):
        component = FakeComponent([FakeCharger("a")], storage=FakeStorage())

        async def accepted(module, key, value, ok=ok):
            """A save that goes through, as None from a test double or True from the real Storage."""
            return ok

        component.storage.save = accepted
        run_async(component.charger_control_save_enabled())
        assert not any("Warn" in line for line in component.logs), component.logs


def test_no_switch_without_storage_settings():
    """A component that passes no storage location has no persisted switch."""
    component = FakeComponent([FakeCharger("a")], storage=FakeStorage())
    component.charger_control_setup("Fake", "charger")
    run_async(component.charger_control_save_enabled())
    run_async(component.charger_control_load_enabled())
    assert component.storage.saved == {}


def test_failed_release_is_retried():
    """A release that raises propagates to the component and is tried again next cycle.

    A charger is only forgotten once its release has gone through, so the retry still knows
    which chargers Predbat was holding - and only the one that refused is released again.
    """
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    _plan(component, 0, [])
    _plan(component, 1, [])
    run_async(component.charger_control_tick(_now()))

    real_release = component.charger_control_release_one

    async def fail_on_b(handle, charge):
        """Release a, refuse b."""
        if handle.key == "b":
            raise OSError("refused")
        await real_release(handle, charge)

    component.charger_control_release_one = fail_on_b
    component.args["set_read_only"] = True
    try:
        run_async(component.charger_control_tick(_now()))
        raise AssertionError("The failed release should reach the component")
    except OSError:
        pass
    # The charger that refused stays held, so it is tried again next cycle; the reason is logged only once
    assert set(component.charger_control_state) == {"b"}, component.charger_control_state

    component.charger_control_release_one = real_release
    run_async(component.charger_control_tick(_now()))
    releases = [command for command in component.commands if command[1] == "release"]
    assert releases == [("a", "release", False), ("b", "release", False)], releases
    assert component.charger_control_state == {} and component.charger_control_released is not None
    assert len([line for line in component.logs if "releasing the charger because of" in line]) == 1, component.logs


def test_one_refusing_charger_does_not_block_the_others():
    """A charger that refuses is retried on its own - the others are still driven and released."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    _plan(component, 0, [])
    _plan(component, 1, [])
    component.refusing = {"a"}
    try:
        run_async(component.charger_control_tick(_now()))
        raise AssertionError("The refusal should still be raised for the run loop to log")
    except RuntimeError:
        pass
    assert component.commands == [("b", "off", 1)], component.commands

    component.refusing = set()
    run_async(component.charger_control_tick(_now()))
    component.refusing = {"a"}
    component.args["set_read_only"] = True
    try:
        run_async(component.charger_control_tick(_now()))
    except RuntimeError:
        pass
    assert ("b", "release", False) in component.commands, component.commands
    assert list(component.charger_control_state) == ["a"], "Only the refusing charger is still held: {}".format(component.charger_control_state)


def test_inactive_does_nothing():
    """Control that was never enabled neither commands nor releases."""
    component = FakeComponent([FakeCharger("a")])
    component.charger_control_active = False
    _plan(component, 0, [])
    component.args["set_read_only"] = True
    run_async(component.charger_control_tick(_now()))
    component.args["set_read_only"] = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


class FakeOctopus:
    """The parts of the Octopus component the Octopus rule reads."""

    def __init__(self, configured=True, automatic=True):
        """configured False means discovery has not wired the car slots yet."""
        self.automatic = automatic
        self.intelligent_config_devices = [] if configured else None


class FakeKraken:
    """The part of the Kraken component the Octopus rule reads."""

    def __init__(self, started):
        """started False means its first run has not succeeded yet."""
        self.api_started = started


class FakeComponents:
    """Component registry holding at most an Octopus and a Kraken component."""

    def __init__(self, octopus, kraken=None):
        """Wrap the given components, or None."""
        self.octopus = octopus
        self.kraken = kraken

    def get_component(self, name):
        """Only Octopus and Kraken are known."""
        return {"octopus": self.octopus, "kraken": self.kraken}.get(name)


DISPATCH = "binary_sensor.predbat_octopus_intelligent_dispatch"


def _octopus_component(octopus=None, is_charger=None, control=None, wired=True):
    """A component for car 0's charger with the given Octopus arrangement.

    is_charger is what car 0's dispatch sensor says, None for a sensor that does not say.
    """
    component = FakeComponent([FakeCharger("a")])
    component.charger_control_config = control
    component.base.components = FakeComponents(octopus)
    component.base.car_slot_owner = None
    if wired:
        component.args["octopus_intelligent_slot"] = [DISPATCH]
        component.sensors[DISPATCH] = {} if is_charger is None else {"is_charger": is_charger}
    _plan(component, 0, [])
    return component


def test_parse_control_setting():
    """Control settings arrive unconverted, so quoted and numeric values must still read right."""
    for value, expected in ((None, None), (True, True), (False, False), ("false", False), ("False", False), ("off", False), ("true", True), ("on", True), (0, False), (1, True)):
        assert parse_control_setting(value) is expected, (value, parse_control_setting(value))


def test_octopus_rule_without_octopus_drives():
    """No Octopus Intelligent car at all - Predbat drives the charger."""
    component = _octopus_component(wired=False)
    assert component.charger_control_octopus_drives_charger(0) is False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


def test_octopus_rule_car_integrated_drives():
    """Octopus drives the car, not the charger - Predbat drives the charger to match the dispatches,
    but only with octopus_intelligent_charger_follows_car on."""
    component = _octopus_component(FakeOctopus(), is_charger=False)
    component.args["octopus_intelligent_charger_follows_car"] = True
    assert component.charger_control_octopus_drives_charger(0) is False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


def test_octopus_rule_car_integrated_left_alone_by_default():
    """Octopus drives the car - with octopus_intelligent_charger_follows_car off (the default) the charger
    is left alone, and one Predbat held stopped is released as usual: nobody else drives the charger."""
    component = _octopus_component(FakeOctopus(), wired=False)
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands

    component.args["octopus_intelligent_slot"] = [DISPATCH]
    component.sensors[DISPATCH] = {"is_charger": False, "state": "on"}
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], "Released once, then left alone: {}".format(component.commands)
    assert component.charger_control_state == {}
    assert sum("drives car 0 itself" in line and "octopus_intelligent_charger_follows_car" in line for line in component.logs) == 1, component.logs

    # With Octopus Intelligent charging off in Predbat, Predbat plans the car and drives the charger to that plan
    component.args["octopus_intelligent_charging"] = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0) and len(component.commands) == 3, component.commands
    del component.args["octopus_intelligent_charging"]
    component.charger_control_state = {}

    # Turned on: Predbat drives the charger to the running dispatch
    component.args["octopus_intelligent_charger_follows_car"] = True
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "on", 0), component.commands
    assert any("no longer left to Octopus" in line for line in component.logs), component.logs


def test_octopus_rule_charge_point_hands_off_without_starting():
    """Octopus drives the charger itself - Predbat lets go of a charger it had stopped without
    starting it, and says so once."""
    component = _octopus_component(wired=False)
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands

    component.args["octopus_intelligent_slot"] = [DISPATCH]
    component.sensors[DISPATCH] = {"is_charger": True}
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], "A stopped charger is left for Octopus, not started: {}".format(component.commands)
    assert component.charger_control_state == {}
    assert sum("leaving it to Octopus" in line for line in component.logs) == 1, component.logs

    # Octopus Intelligent turned off in Predbat only changes Predbat's planning - Octopus still
    # drives the charger, so Predbat still leaves it alone rather than fight
    component.args["octopus_intelligent_charging"] = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands

    # The charger stops being the Octopus device, and the user has Predbat follow the car - Predbat takes it back
    component.args["octopus_intelligent_charger_follows_car"] = True
    component.sensors[DISPATCH] = {"is_charger": False}
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0) and len(component.commands) == 2, component.commands
    assert any("no longer left to Octopus" in line for line in component.logs), component.logs


def test_octopus_rule_charge_point_releases_a_running_charger():
    """A charger Predbat had running is released as usual when Octopus takes it over."""
    component = _octopus_component(wired=False)
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0)], component.commands

    component.args["octopus_intelligent_slot"] = [DISPATCH]
    component.sensors[DISPATCH] = {"is_charger": True}
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("a", "release", True)], component.commands


def test_octopus_rule_car_integrated_follows_the_dispatch_sensor():
    """Octopus drives the car - the charger runs while a dispatch is on, even before the plan shows it."""
    component = _octopus_component(FakeOctopus(), is_charger=False)
    component.args["octopus_intelligent_charger_follows_car"] = True
    component.sensors[DISPATCH]["state"] = "on"
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0)], component.commands

    component.sensors[DISPATCH]["state"] = "off"
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0), component.commands

    # With octopus_intelligent_charging off Predbat follows only its own plan
    component.args["octopus_intelligent_charging"] = False
    component.sensors[DISPATCH]["state"] = "on"
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0) and len(component.commands) == 2, component.commands


def test_dispatch_times_judged_against_the_clock():
    """The dispatch times on the sensor decide, not its on/off state, which lags the end of a
    dispatch by up to a refresh. Octopus and Kraken write the times differently."""
    component = _octopus_component(FakeOctopus(), is_charger=False)
    now = _now()  # 01:30 London, 00:30 UTC
    component.sensors[DISPATCH]["state"] = "on"
    # Octopus format - ended at 00:30 UTC, so the stale "on" must not keep the charger running
    component.sensors[DISPATCH]["completed_dispatches"] = [{"start": "2026-06-01T00:00:00+0000", "end": "2026-06-01T00:30:00+0000"}]
    assert component.charger_control_dispatch_active(0, now) is False
    # Kraken format - running now
    component.sensors[DISPATCH]["planned_dispatches"] = [{"start": "2026-06-01T00:15:00Z", "end": "2026-06-01T01:00:00Z"}]
    assert component.charger_control_dispatch_active(0, now) is True
    # A sensor with no dispatch times falls back to its state
    component.sensors[DISPATCH] = {"is_charger": False, "state": "on"}
    assert component.charger_control_dispatch_active(0, now) is True
    assert parse_dispatch_time("not a time") is None and parse_dispatch_time(None) is None
    # No offset: read as UTC rather than raising when compared with the clock
    component.sensors[DISPATCH] = {"is_charger": False, "state": "off", "planned_dispatches": [{"start": "2026-06-01T00:15:00", "end": "2026-06-01T01:00:00"}]}
    assert component.charger_control_dispatch_active(0, now) is True


PLANNED = "binary_sensor.car_planned_dispatches"


def test_planned_slot_sensor_replaces_the_slot_sensor_plan():
    """With octopus_intelligent_planned_slot set, its dispatches are the plan the charger follows, as they are
    for the car's plan, so a dispatch it leaves out does not run the charger from the slot sensor."""
    # The charger whose Octopus arrangement cannot be told, with control set to true
    component = _octopus_component(control=True)
    running = [{"start": "2026-06-01T00:00:00+00:00", "end": "2026-06-01T01:00:00+00:00"}]
    component.sensors[DISPATCH].update({"state": "on", "planned_dispatches": running})
    component.args["octopus_intelligent_planned_slot"] = [PLANNED]
    component.sensors[PLANNED] = {"state": "on", "planned_dispatches": []}
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands

    # Nothing on either sensor: the slot sensor's state does not stand in for the planned sensor's empty plan
    component.sensors[DISPATCH]["planned_dispatches"] = []
    assert component.charger_control_dispatch_active(0, _now()) is False

    # The planned sensor keeps the dispatch, and the charger runs before the plan shows it
    component.sensors[PLANNED]["planned_dispatches"] = running
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "on", 0)], component.commands

    # One the planned sensor has seen finish counts too, as it does for the car's plan
    component.sensors[PLANNED] = {"state": "off", "planned_dispatches": [], "completed_dispatches": running}
    assert component.charger_control_dispatch_active(0, _now()) is True

    # Naming the slot sensor itself, or a regex that matched nothing, leaves the slot sensor as the plan
    component.sensors[DISPATCH]["planned_dispatches"] = running
    component.sensors[PLANNED] = {"state": "off", "planned_dispatches": []}
    for planned in (DISPATCH, [DISPATCH], ["re:(binary_sensor.nothing)"], [None]):
        component.args["octopus_intelligent_planned_slot"] = planned
        assert component.charger_control_dispatch_active(0, _now()) is True, planned

    # The car plan does not read the planned sensor while an action config supplies the slots, so neither does the charger
    component.args["octopus_intelligent_planned_slot"] = [PLANNED]
    component.args["octopus_intelligent_slot_action_config"] = {"device_id": "x"}
    assert component.charger_control_dispatch_active(0, _now()) is True


def test_octopus_charger_filtered_through_a_planned_sensor():
    """Octopus drives the charger, but with control: true and octopus_intelligent_planned_slot set Predbat
    stops it outside the dispatches the planned sensor keeps."""
    running = [{"start": "2026-06-01T00:00:00+00:00", "end": "2026-06-01T01:00:00+00:00"}]
    component = _octopus_component(FakeOctopus(), is_charger=True, control=True)
    component.sensors[DISPATCH].update({"state": "on", "planned_dispatches": running})

    # No planned sensor: left to Octopus, control: true or not
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands
    assert any("leaving it to Octopus" in line for line in component.logs), component.logs

    # The planned sensor has left the running dispatch out, so Predbat stops the charger
    component.args["octopus_intelligent_planned_slot"] = PLANNED
    component.sensors[PLANNED] = {"state": "on", "planned_dispatches": []}
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    assert any("no longer left to Octopus" in line for line in component.logs), component.logs
    component.sensors[PLANNED]["planned_dispatches"] = running
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "on", 0)], component.commands

    # Control back to unset: handed back to Octopus as usual
    component.charger_control_config = None
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "release", True) and component.charger_control_state == {}, component.commands

    # Car 0's slots claimed by the Ohme stay the Ohme's
    component = _octopus_component(control=True)
    component.base.car_slot_owner = "ohme"
    component.args["octopus_intelligent_planned_slot"] = PLANNED
    component.sensors[PLANNED] = {"state": "on", "planned_dispatches": []}
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


def test_dispatches_the_planned_sensor_drops_get_the_day_rate(my_predbat):
    """A raw dispatch the planned sensor drops is one the filtered charger never runs in, so its minutes go back to
    the day rate, from the current half hour unless the car has drawn in it. The fixed window, a dispatch the
    planned sensor keeps and a car that is not filtered are unchanged."""
    saved = {
        name: getattr(my_predbat, name)
        for name in (
            "components",
            "minutes_now",
            "num_cars",
            "octopus_slots",
            "octopus_slots_raw_planned",
            "octopus_slots_held",
            "car_charging_now",
            "octopus_intelligent_consider_full",
            "dispatch_billing_window",
            "io_adjusted",
            "octopus_surplus",
            "rate_max_base",
        )
    }

    def dispatch(start_hour, end_hour):
        """A raw dispatch from start_hour to end_hour today."""
        start = my_predbat.midnight_utc + datetime.timedelta(hours=start_hour)
        end = my_predbat.midnight_utc + datetime.timedelta(hours=end_hour)
        return {"start": start.strftime("%Y-%m-%dT%H:%M:%S%z"), "end": end.strftime("%Y-%m-%dT%H:%M:%S%z"), "charge_in_kwh": 5.0, "source": "smart-charge", "location": "AT_HOME"}

    def skipped(component, raw, kept, minutes_now=10 * 60, drew=False):
        """The minutes octopus_surplus_minutes() strips with the charger under component, and the rates it leaves."""
        my_predbat.components.components = {"fake": component}
        my_predbat.minutes_now = minutes_now
        my_predbat.octopus_slots = [kept]
        my_predbat.octopus_slots_raw_planned = {0: raw}
        my_predbat.car_charging_now = [drew]
        return my_predbat.octopus_surplus_minutes()

    try:
        my_predbat.components = type("Components", (), {})()
        my_predbat.num_cars = 1
        my_predbat.octopus_slots_held = {}
        my_predbat.octopus_intelligent_consider_full = False
        my_predbat.dispatch_billing_window = lambda: False
        component = _octopus_component(FakeOctopus(), is_charger=True, control=True)
        component.args["octopus_intelligent_planned_slot"] = PLANNED
        kept = dispatch(12, 13)
        dropped = dispatch(14, 15)
        raw = [kept, dropped]

        # In the future: the dropped dispatch is stripped, the kept one is not
        assert skipped(component, raw, [kept]) == set(range(14 * 60, 15 * 60))

        # The current half hour with no draw is stripped too, and with a draw it keeps the dispatch rate
        assert skipped(component, raw, [kept], minutes_now=14 * 60 + 10) == set(range(14 * 60, 15 * 60))
        assert skipped(component, raw, [kept], minutes_now=14 * 60 + 10, drew=True) == set(range(14 * 60 + 30, 15 * 60))
        my_predbat.octopus_slots_held = {0: {my_predbat.midnight_utc + datetime.timedelta(hours=14): "smart-charge"}}
        assert skipped(component, raw, [kept], minutes_now=14 * 60 + 10) == set(range(14 * 60 + 30, 15 * 60))
        my_predbat.octopus_slots_held = {}

        # The fixed 23:30-05:30 window stays cheap by tariff
        assert skipped(component, [dispatch(23, 24)], []) == set(range(23 * 60, 23 * 60 + 30))

        # The feed's rate is put back on the stripped minutes only
        my_predbat.octopus_surplus = skipped(component, raw, [kept])
        my_predbat.rate_max_base = 30.0
        my_predbat.io_adjusted = {minute: True for minute in range(12 * 60, 15 * 60)}
        rates = my_predbat.dynamic_load_car_strip_feed_rates({minute: 5.0 for minute in range(12 * 60, 15 * 60)})
        assert {rates[minute] for minute in range(12 * 60, 13 * 60)} == {5.0} and {rates[minute] for minute in range(14 * 60, 15 * 60)} == {30.0}, rates

        # Not filtered: control off, or no planned sensor
        component.charger_control_config = False
        assert skipped(component, raw, [kept]) == set()
        component.charger_control_config = True
        del component.args["octopus_intelligent_planned_slot"]
        assert skipped(component, raw, [kept]) == set()
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_is_charger_that_has_lost_its_value_is_not_a_no():
    """A dispatch sensor reporting is_charger as unknown or unavailable is cannot tell, not 'not the charger'."""
    for lost in ("unknown", "unavailable", "none", ""):
        component = _octopus_component(FakeOctopus(), is_charger=lost)
        assert component.charger_control_octopus_drives_charger(0) is None, lost


def test_guest_switch_toggle():
    """A toggle flips guest charging rather than turning it off."""
    component = FakeComponent([FakeCharger("a")])
    component.charger_control_switch_prefix = "fake"
    run_async(component.charger_control_guest_event("switch.predbat_fake_guest_charging", "toggle"))
    assert component.charger_control_guest is True
    run_async(component.charger_control_guest_event("switch.predbat_fake_guest_charging", "toggle"))
    assert component.charger_control_guest is False


def test_octopus_rule_explicit_true_never_overrides_a_charge_point():
    """control: true does not make Predbat fight Octopus for a charger Octopus is known to drive."""
    component = _octopus_component(FakeOctopus(), is_charger=True, control=True)
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


def test_octopus_rule_unknown_hands_off_unless_told():
    """A dispatch sensor that does not say (the Octopus Energy integration) - hands off unless control: true."""
    component = _octopus_component(None)
    assert component.charger_control_octopus_drives_charger(0) is None
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands
    warnings = [line for line in component.logs if line.startswith("Warn") and "cannot tell" in line]
    assert len(warnings) == 1, component.logs
    assert not any("leaving it to Octopus" in line for line in component.logs), component.logs
    run_async(component.charger_control_tick(_now()))
    assert sum(line.startswith("Warn") for line in component.logs) == 1, "Warned once, not every cycle: {}".format(component.logs)

    named = _octopus_component(None)
    named.charger_control_setting = "ge_cloud_evc_control"
    run_async(named.charger_control_tick(_now()))
    assert any("Set ge_cloud_evc_control: true" in line for line in named.logs), named.logs

    told = _octopus_component(None, control=True)
    run_async(told.charger_control_tick(_now()))
    assert told.commands == [("a", "off", 0)], told.commands


def test_octopus_rule_waits_for_octopus_discovery():
    """The Octopus component has not wired its devices yet - nothing is commanded until it has."""
    component = _octopus_component(FakeOctopus(configured=False), wired=False)
    assert component.charger_control_octopus_drives_charger(0) is None
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands
    assert any("waiting for the Octopus or Kraken component" in line for line in component.logs), component.logs
    assert not any(line.startswith("Warn") for line in component.logs), "Waiting for discovery is not worth a warning: {}".format(component.logs)

    # Discovery found no Intelligent devices - there is nothing for Octopus to drive
    component.base.components = FakeComponents(FakeOctopus())
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


def test_octopus_rule_waits_for_kraken():
    """Kraken wires its slots in its first successful run - until then nothing is commanded."""
    component = _octopus_component(None, wired=False)
    component.base.components = FakeComponents(None, FakeKraken(started=False))
    assert component.charger_control_octopus_drives_charger(0) is None
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands

    component.base.components = FakeComponents(None, FakeKraken(started=True))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], "Kraken started and wired nothing for this car: {}".format(component.commands)


def test_octopus_rule_discovery_wait_is_bounded():
    """A Kraken component whose first run never succeeds (a failed login, say) is not waited on for good:
    once the wait is over Predbat cannot tell, warns, and hands back a charger it holds - never driving it,
    as Kraken may be the one driving it."""
    component = _octopus_component(wired=False)
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands

    # Kraken restarts: the charger Predbat stopped is left as it is, and still held
    component.base.components = FakeComponents(None, FakeKraken(started=False))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], "Nothing sent while discovery is under way: {}".format(component.commands)
    assert component.charger_control_state == {"a": False}, component.charger_control_state
    assert any("waiting for the Octopus or Kraken component" in line for line in component.logs), component.logs
    assert not any(line.startswith("Warn") for line in component.logs), component.logs

    # It never comes back
    component.charger_control_discovery_since = {0: datetime.datetime.now() - datetime.timedelta(minutes=OCTOPUS_DISCOVERY_WAIT_MINUTES + 1)}
    assert component.charger_control_octopus_drives_charger(0) is None, "Still cannot tell, so Predbat does not take it over"
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], "The stop is undone rather than left: {}".format(component.commands)
    assert component.charger_control_state == {}
    assert sum(line.startswith("Warn") and "has not found its devices" in line for line in component.logs) == 1, component.logs
    run_async(component.charger_control_tick(_now()))
    assert len(component.commands) == 2 and sum(line.startswith("Warn") for line in component.logs) == 1, "Left alone, and warned once: {}".format(component.logs)

    # Discovery that finishes resets the wait, so a later restart of the component waits afresh
    component.base.components = FakeComponents(None, FakeKraken(started=True))
    component.charger_control_octopus_discovering()
    assert component.charger_control_discovery_since == {}
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0), "Predbat drives it again once discovery has wired nothing for it: {}".format(component.commands)

    # A wait left over from before a charger's driver became known does not cut the next wait short
    wired = _octopus_component(None, is_charger=False)
    wired.charger_control_discovery_since = {0: datetime.datetime.now() - datetime.timedelta(minutes=OCTOPUS_DISCOVERY_WAIT_MINUTES + 1)}
    run_async(wired.charger_control_tick(_now()))
    assert 0 not in wired.charger_control_discovery_since, wired.charger_control_discovery_since


def test_octopus_rule_discovery_wait_is_per_car():
    """One car whose driver is known does not restart the wait of another car still waiting on discovery,
    so the waiting car still reaches "cannot tell" once its wait is over."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    component.base.components = FakeComponents(None, FakeKraken(started=False))
    component.base.car_slot_owner = None
    # Car 0 is wired and Octopus drives the car, which Predbat is asked to follow; car 1 is not wired yet
    component.args["octopus_intelligent_slot"] = [DISPATCH]
    component.sensors[DISPATCH] = {"is_charger": False}
    component.args["octopus_intelligent_charger_follows_car"] = True
    _plan(component, 0, [])
    _plan(component, 1, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    assert 1 in component.charger_control_discovery_since and 0 not in component.charger_control_discovery_since, component.charger_control_discovery_since
    assert not any(line.startswith("Warn") for line in component.logs), component.logs

    # Car 1's wait runs out, while car 0 goes on being driven every cycle
    component.charger_control_discovery_since[1] = datetime.datetime.now() - datetime.timedelta(minutes=OCTOPUS_DISCOVERY_WAIT_MINUTES + 1)
    run_async(component.charger_control_tick(_now()))
    assert sum(line.startswith("Warn") and "has not found its devices" in line and "car 1" in line for line in component.logs) == 1, component.logs


def test_octopus_rule_cannot_tell_releases_a_held_charger():
    """A charger Predbat holds stopped is released, not just let go, when nobody is known to take it over."""
    component = _octopus_component(wired=False)
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    component.args["octopus_intelligent_slot"] = [DISPATCH]
    component.sensors[DISPATCH] = {}
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], "A stopped charger is started again, not stranded: {}".format(component.commands)
    assert component.charger_control_state == {}


def test_octopus_rule_unmatched_regex_is_not_a_sensor():
    """The apps.yaml default's literal "re:" string, before its regex has matched, is not a dispatch sensor to ask."""
    component = _octopus_component(wired=False)
    component.args["octopus_intelligent_slot"] = ["re:(binary_sensor.octopus_energy_([0-9a-z_]+|)_intelligent_dispatching)"]
    assert component.charger_control_octopus_drives_charger(0) is False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    assert not any(line.startswith("Warn") for line in component.logs), component.logs


def test_octopus_rule_does_not_wait_without_octopus_automatic():
    """With octopus_automatic off the Octopus component never wires the slots, so there is nothing to wait for."""
    component = _octopus_component(FakeOctopus(configured=False, automatic=False), wired=False)
    assert component.charger_control_octopus_drives_charger(0) is False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


def test_octopus_rule_other_slot_owner_hands_off():
    """Another component (Ohme) supplies the Intelligent slots from the charger itself."""
    component = _octopus_component(None)
    component.base.car_slot_owner = "ohme"
    assert component.charger_control_octopus_drives_charger(0) is True


def test_octopus_rule_per_car():
    """Each car is judged on its own wired dispatch sensor, whatever order the slots are listed in
    and whichever component (Octopus or Kraken) published them."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    component.base.components = FakeComponents(None)
    component.base.car_slot_owner = None
    component.args["octopus_intelligent_slot"] = ["binary_sensor.predbat_kraken_intelligent_dispatch_z", "binary_sensor.predbat_kraken_intelligent_dispatch_a"]
    component.sensors = {
        "binary_sensor.predbat_kraken_intelligent_dispatch_z": {"is_charger": True},
        "binary_sensor.predbat_kraken_intelligent_dispatch_a": {"is_charger": False},
    }
    component.args["octopus_intelligent_charger_follows_car"] = True
    _plan(component, 0, [])
    _plan(component, 1, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("b", "off", 1)], component.commands


def test_octopus_rule_other_slot_owner_second_car():
    """Since #5443 Octopus's own dispatch sensors stay wired while Ohme holds car 0's slot claim, so
    a second car is judged on what its own sensor says rather than handed to Octopus with the Ohme."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    component.base.components = FakeComponents(None)
    component.base.car_slot_owner = "ohme"
    dispatch_b = "binary_sensor.predbat_octopus_intelligent_dispatch_b"
    component.args["octopus_intelligent_slot"] = [DISPATCH, dispatch_b]
    component.sensors = {DISPATCH: {"is_charger": True}, dispatch_b: {"is_charger": False}}
    assert component.charger_control_octopus_drives_charger(0) is True
    assert component.charger_control_octopus_drives_charger(1) is False
    assert component.charger_control_octopus_drives_car(0) is False
    assert component.charger_control_octopus_drives_car(1) is True

    # The claim alone settles car 0, whatever its sensor says - the Ohme's own slot sensor says
    # nothing, and the claim may be forced on with ohme_automatic_octopus_intelligent
    component.sensors = {DISPATCH: {"is_charger": False}, dispatch_b: {"is_charger": "unknown"}}
    assert component.charger_control_octopus_drives_charger(0) is True
    assert component.charger_control_octopus_drives_car(0) is False
    # A second car whose sensor has lost its value cannot be told, rather than handed to Octopus with the Ohme
    assert component.charger_control_octopus_drives_charger(1) is None
    assert component.charger_control_octopus_drives_car(1) is False

    # Following the car, Predbat drives the second car's charger to its dispatches and leaves the Ohme's alone
    component.sensors = {DISPATCH: {"is_charger": True}, dispatch_b: {"is_charger": False}}
    component.args["octopus_intelligent_charger_follows_car"] = True
    _plan(component, 0, [])
    _plan(component, 1, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("b", "off", 1)], component.commands


def test_unpublished_plan_dispatch_charge_stops_when_the_dispatch_ends():
    """A dispatch starts a car whose plan is not published yet; when the dispatch ends the charger
    Predbat now holds is stopped, not left running with nobody in control."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    component.base.components = FakeComponents(None)
    component.base.car_slot_owner = None
    dispatch_b = "binary_sensor.predbat_octopus_intelligent_dispatch_b"
    component.args["octopus_intelligent_slot"] = [DISPATCH, dispatch_b]
    component.sensors = {DISPATCH: {"is_charger": False}, dispatch_b: {"is_charger": False, "state": "on"}}
    component.args["octopus_intelligent_charger_follows_car"] = True
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("b", "on", 1)], component.commands

    component.sensors[dispatch_b]["state"] = "off"
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("b", "off", 1), component.commands


def test_plan_that_goes_missing_keeps_its_last_windows():
    """A plan read once and then missing for a cycle keeps its windows, so a held charger is not
    stopped mid-window just because the slot sensor lost its planned attribute."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    component.base.num_cars = 2
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    _plan(component, 1, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("b", "off", 1)], component.commands

    del component.plans["binary_sensor.predbat_car_charging_slot"]
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("b", "off", 1)], component.commands
    # ...and the last windows are still judged against the clock
    run_async(component.charger_control_tick(_now() + datetime.timedelta(hours=1)))
    assert component.commands[-1] == ("a", "off", 0), component.commands


def _guest_component():
    """A component with a guest switch, holding its charger stopped outside any window."""
    component = FakeComponent([FakeCharger("a")])
    component.charger_control_switch_prefix = "fake"
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    return component


def test_guest_charging_releases_and_resumes():
    """Guest charging hands the charger back so the guest can charge, and Predbat takes it back after."""
    component = _guest_component()
    assert run_async(component.charger_control_guest_event("switch.predbat_fake_guest_charging", "turn_on")) is True
    assert run_async(component.charger_control_guest_event("switch.predbat_fake_other", "turn_on")) is False
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], component.commands

    run_async(component.charger_control_guest_event("switch.predbat_fake_guest_charging", "turn_off"))
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0), component.commands


def test_guest_charging_ends_when_the_car_is_unplugged():
    """A charger that can tell ends guest charging when a connected car is unplugged - but not
    before the guest has plugged in."""
    component = _guest_component()
    charger = component.chargers[0]
    charger.connected = False
    component.charger_control_set_guest(True)
    run_async(component.charger_control_tick(_now()))
    assert component.charger_control_guest is True, "Turned on before the guest arrived, so nothing has been unplugged yet"

    charger.connected = True
    run_async(component.charger_control_tick(_now()))
    charger.connected = False
    run_async(component.charger_control_tick(_now()))
    assert component.charger_control_guest is False
    assert any("the guest's car was unplugged" in line for line in component.logs), component.logs


def test_guest_charging_survives_the_owner_unplugging():
    """The owner's car, already on the charger when guest charging went on, is unplugged to make
    way for the guest - that must not end guest charging."""
    component = _guest_component()
    charger = component.chargers[0]
    component.charger_control_set_guest(True)
    run_async(component.charger_control_tick(_now()))
    charger.connected = False
    run_async(component.charger_control_tick(_now()))
    assert component.charger_control_guest is True, "The owner's unplug must not end guest charging"

    # The guest plugs in, charges, and leaves
    charger.connected = True
    run_async(component.charger_control_tick(_now()))
    charger.connected = False
    run_async(component.charger_control_tick(_now()))
    assert component.charger_control_guest is False


def test_guest_charging_times_out():
    """A charger that cannot tell a car was unplugged ends guest charging after the time limit."""
    component = _guest_component()
    component.charger_control_set_guest(True)
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now() + datetime.timedelta(hours=GUEST_CHARGING_MAX_HOURS - 1)))
    assert component.charger_control_guest is True
    run_async(component.charger_control_tick(_now() + datetime.timedelta(hours=GUEST_CHARGING_MAX_HOURS)))
    assert component.charger_control_guest is False
    assert component.commands[-1] == ("a", "off", 0), "Predbat drives the charger again: {}".format(component.commands)


def test_no_guest_switch_without_a_prefix():
    """A component that did not ask for a guest switch has none."""
    component = FakeComponent([FakeCharger("a")])
    assert component.charger_control_guest_entity() is None
    assert run_async(component.charger_control_guest_event("switch.predbat_fake_guest_charging", "turn_on")) is False


def test_held_chargers_survive_a_restart():
    """A charger Predbat stopped is saved, so after a restart read only still releases it."""
    storage = FakeStorage()
    component = FakeComponent([FakeCharger("a")], storage=storage)
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)]
    assert storage.saved[("fake", "control_state")]["held"] == {"a": False}

    restarted = FakeComponent([FakeCharger("a")], storage=storage)
    run_async(restarted.charger_control_load_enabled())
    assert restarted.charger_control_state == {"a": False}
    # reset() leaves the base attribute True until the first update_pred(): not read only, so nothing is
    # released - the restored stop is only sent again, as it is not confirmed
    restarted.base.set_read_only = True
    _plan(restarted, 0, [])
    run_async(restarted.charger_control_tick(_now()))
    assert restarted.commands == [("a", "off", 0)], restarted.commands
    restarted.args["set_read_only"] = True
    run_async(restarted.charger_control_tick(_now()))
    assert restarted.commands == [("a", "off", 0), ("a", "release", False)], restarted.commands
    assert storage.saved[("fake", "control_state")]["held"] == {}


def test_restored_hold_released_when_control_is_off():
    """Control since turned off in apps.yaml: a charger an earlier session held is released, once."""
    storage = FakeStorage({("fake", "control_state"): {"enabled": True, "held": {"a": False}}})
    component = FakeComponent([FakeCharger("a")], storage=storage)
    run_async(component.charger_control_load_enabled())
    component.charger_control_active = False
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "release", False)], component.commands
    assert len([line for line in component.logs if "control is not enabled" in line]) == 1, component.logs


def test_stop_recorded_before_it_is_sent():
    """A stop that fails is still held, sent again next cycle, and released by read only."""
    storage = FakeStorage()
    charger = FakeCharger("a")
    component = FakeComponent([charger], storage=storage)
    _plan(component, 0, [])
    component.refusing.add("a")
    try:
        run_async(component.charger_control_tick(_now()))
        raise AssertionError("The refused stop should reach the component")
    except RuntimeError:
        pass
    assert component.charger_control_state == {"a": False}
    assert storage.saved[("fake", "control_state")]["held"] == {"a": False}
    component.refusing.discard("a")
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], "An unconfirmed stop is sent again"
    assert not any("changed away" in line for line in component.logs), component.logs
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], "Once confirmed it is not sent again"


def test_deferred_release_is_kept_quietly():
    """A release the component defers keeps the charger held, reports nothing, and is retried each cycle."""
    component = FakeComponent([FakeCharger("a")])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    real_release = component.charger_control_release_one
    calls = []

    async def defer(handle, charge):
        """Not yet."""
        calls.append(handle.key)
        raise ChargerControlDeferred()

    component.charger_control_release_one = defer
    component.args["set_read_only"] = True
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert calls == ["a", "a"] and component.charger_control_state == {"a": False}
    component.charger_control_release_one = real_release
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "release", False) and component.charger_control_state == {}
    assert len([line for line in component.logs if "releasing the charger because of" in line]) == 1, component.logs


def test_unplug_releases_a_stopped_charger():
    """A charger Predbat stopped is released when it reports its car unplugged."""
    charger = FakeCharger("a")
    component = FakeComponent([charger])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    charger.unplugged = True
    charger.connected = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], component.commands
    assert component.charger_control_state == {}
    run_async(component.charger_control_tick(_now()))
    assert len(component.commands) == 2


def test_restored_hold_is_reapplied_once():
    """After a restart a restored hold is not trusted: the plan's command is sent once more, then not again."""
    storage = FakeStorage({("fake", "control_state"): {"enabled": True, "held": {"a": False}}})
    component = FakeComponent([FakeCharger("a")], storage=storage)
    run_async(component.charger_control_load_enabled())
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands
    assert not any("changed away" in line for line in component.logs), component.logs


def test_unplugged_charger_is_not_driven():
    """A charger that reports its car unplugged is not stopped - it would only be released again next cycle."""
    charger = FakeCharger("a")
    charger.unplugged = True
    component = FakeComponent([charger])
    _plan(component, 0, [])
    for _ in range(3):
        run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


def test_held_charger_released_when_unplugged_either_way():
    """A charger Predbat left allowed is handed back when its car is unplugged too, not held allowed for the next car."""
    charger = FakeCharger("a")
    component = FakeComponent([charger])
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    charger.unplugged = True
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("a", "release", True)], component.commands
    assert component.charger_control_state == {}


def test_restored_hold_waits_for_its_plan():
    """After a restart a restored hold is not driven until its own car's plan is published."""
    storage = FakeStorage({("fake", "control_state"): {"enabled": True, "held": {"b": True}}})
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")], storage=storage)
    component.base.num_cars = 2
    run_async(component.charger_control_load_enabled())
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    assert ("b", "off", 1) not in component.commands, component.commands


def test_failed_save_is_retried():
    """Saves happen only when a hold changes, but one that failed is tried again at the start of the next cycle."""
    storage = FakeStorage(fail=True)
    component = FakeComponent([FakeCharger("a")], storage=storage)
    component.refusing.add("a")
    _plan(component, 0, [])
    for _ in range(2):
        try:
            run_async(component.charger_control_tick(_now()))
        except RuntimeError:
            pass
    assert storage.saves == 2, "A failed save is retried while the stop is resent"
    storage.fail = False
    component.refusing.discard("a")
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert storage.saved[("fake", "control_state")]["held"] == {"a": False}
    saves = storage.saves
    run_async(component.charger_control_tick(_now()))
    assert storage.saves == saves, "Nothing changed, so nothing is saved"


def test_control_off_hands_an_octopus_charger_to_octopus():
    """With control off, a held charger Octopus now drives is handed to Octopus - a stop left for it to start -
    not released, which would start a charge outside its dispatches."""
    storage = FakeStorage({("fake", "control_state"): {"enabled": True, "held": {"a": False}}})
    component = FakeComponent([FakeCharger("a")], storage=storage)
    run_async(component.charger_control_load_enabled())
    component.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_dispatching"]
    component.sensors["binary_sensor.octopus_dispatching"] = {"is_charger": True}
    component.charger_control_active = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], "The stop is left for Octopus, got {}".format(component.commands)
    assert component.charger_control_state == {}


def test_failed_load_is_retried_by_the_tick():
    """A start-up read that failed is tried again on the next tick, so a held charger is not forgotten."""
    storage = FakeStorage({("fake", "control_state"): {"enabled": True, "held": {"a": False}}}, fail=True)
    component = FakeComponent([FakeCharger("a")], storage=storage)
    assert run_async(component.charger_control_load_enabled()) is False
    assert component.charger_control_loaded is False
    storage.fail = False
    component.charger_control_active = False
    run_async(component.charger_control_tick(_now()))
    assert component.charger_control_loaded is True
    assert component.commands == [("a", "release", False)], component.commands


def run_car_charger_control_tests(my_predbat=None):
    """Run the shared charger control tests. Returns True on failure."""
    print("**** Running car charger control tests ****")
    test_held_chargers_survive_a_restart()
    test_restored_hold_released_when_control_is_off()
    test_stop_recorded_before_it_is_sent()
    test_deferred_release_is_kept_quietly()
    test_unplug_releases_a_stopped_charger()
    test_restored_hold_is_reapplied_once()
    test_unplugged_charger_is_not_driven()
    test_held_charger_released_when_unplugged_either_way()
    test_restored_hold_waits_for_its_plan()
    test_failed_save_is_retried()
    test_control_off_hands_an_octopus_charger_to_octopus()
    test_failed_load_is_retried_by_the_tick()
    test_no_plan_sends_nothing()
    test_on_inside_window_off_outside()
    test_empty_plan_stops()
    test_each_charger_follows_its_own_car()
    test_car_whose_plan_is_not_published_yet_is_left_alone()
    test_charger_without_a_car_is_left_alone()
    test_charger_whose_car_goes_is_released()
    test_disconnected_charger_is_left_alone()
    test_drift_is_reapplied()
    test_read_only_releases_once_and_resumes()
    test_read_only_comes_from_the_setting_and_axle()
    test_switch_off_releases_and_persists()
    test_storage_failures_fail_soft()
    test_storage_save_returning_false_is_a_failed_save()
    test_no_switch_without_storage_settings()
    test_failed_release_is_retried()
    test_one_refusing_charger_does_not_block_the_others()
    test_inactive_does_nothing()
    test_parse_control_setting()
    test_octopus_rule_without_octopus_drives()
    test_octopus_rule_car_integrated_drives()
    test_octopus_rule_car_integrated_left_alone_by_default()
    test_octopus_rule_charge_point_hands_off_without_starting()
    test_octopus_rule_charge_point_releases_a_running_charger()
    test_octopus_rule_car_integrated_follows_the_dispatch_sensor()
    test_dispatch_times_judged_against_the_clock()
    test_planned_slot_sensor_replaces_the_slot_sensor_plan()
    test_octopus_charger_filtered_through_a_planned_sensor()
    if my_predbat:
        test_dispatches_the_planned_sensor_drops_get_the_day_rate(my_predbat)
    test_is_charger_that_has_lost_its_value_is_not_a_no()
    test_guest_switch_toggle()
    test_octopus_rule_explicit_true_never_overrides_a_charge_point()
    test_octopus_rule_unknown_hands_off_unless_told()
    test_octopus_rule_waits_for_octopus_discovery()
    test_octopus_rule_does_not_wait_without_octopus_automatic()
    test_octopus_rule_waits_for_kraken()
    test_octopus_rule_discovery_wait_is_bounded()
    test_octopus_rule_discovery_wait_is_per_car()
    test_octopus_rule_cannot_tell_releases_a_held_charger()
    test_octopus_rule_unmatched_regex_is_not_a_sensor()
    test_octopus_rule_other_slot_owner_hands_off()
    test_octopus_rule_per_car()
    test_octopus_rule_other_slot_owner_second_car()
    test_unpublished_plan_dispatch_charge_stops_when_the_dispatch_ends()
    test_plan_that_goes_missing_keeps_its_last_windows()
    test_guest_charging_releases_and_resumes()
    test_guest_charging_ends_when_the_car_is_unplugged()
    test_guest_charging_survives_the_owner_unplugging()
    test_guest_charging_times_out()
    test_no_guest_switch_without_a_prefix()
    return False
