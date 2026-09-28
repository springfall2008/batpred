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
from car_charger_control import CarChargerControl

LONDON = pytz.timezone("Europe/London")


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self, saved=None, fail=False):
        """Start with an optional saved record, or fail every call."""
        self.saved = dict(saved or {})
        self.fail = fail

    async def save(self, module, key, value):
        """Record a save."""
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
        self.num_cars = 1


class FakeCharger:
    """A charger that can tell whether a car is connected and what state it is in."""

    def __init__(self, key, connected=True):
        """A charger that is currently off."""
        self.key = key
        self.connected = connected
        self.charging = False


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
        self.logs = []
        self.commands = []
        self.charger_control_setup("Fake", "charger", "fake", "control_state", "enabled")
        self.charger_control_active = True

    @property
    def num_cars(self):
        """Mirror ComponentBase.num_cars."""
        return self.base.num_cars

    def log(self, message):
        """Record a log line."""
        self.logs.append(message)

    def get_arg(self, name, default=None):
        """Config args, as ComponentBase.get_arg."""
        return self.args.get(name, default)

    def get_state_wrapper(self, entity_id, default=None, attribute=None):
        """Only the car charging slot sensors are read."""
        return self.plans.get(entity_id, default)

    def charger_control_chargers(self):
        """Chargers in car order."""
        return [(charger.key, charger) for charger in self.chargers]

    def charger_control_connected(self, handle):
        """Use the fake's plug state."""
        return handle.connected

    def charger_control_drifted(self, handle, charge):
        """Only when the test asks for a drift-aware charger."""
        return self.drift_aware and handle.charging != charge

    async def charger_control_send(self, handle, charge, car_n):
        """Record and apply the command."""
        self.commands.append((handle.key, "on" if charge else "off", car_n))
        handle.charging = charge

    async def charger_control_release_one(self, handle, charge):
        """Record the release."""
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


def test_charger_without_a_car_is_left_alone():
    """A charger beyond num_cars has no plan of its own and must not be stopped."""
    component = FakeComponent([FakeCharger("a"), FakeCharger("b")])
    _plan(component, 0, [])
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0)], component.commands


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
    component.base.set_read_only = True
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "off", 0), ("a", "release", False)], component.commands
    assert component.charger_control_state == {}, "Released chargers are forgotten"
    component.base.set_read_only = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands[-1] == ("a", "off", 0), component.commands
    assert any("Read only mode cleared" in line for line in component.logs), component.logs


def test_read_only_falls_back_to_the_arg():
    """Before the base attribute is set the config arg decides."""
    component = FakeComponent([FakeCharger("a")])
    component.base.set_read_only = None
    component.args["set_read_only"] = True
    assert component.charger_control_read_only_now() is True
    component.args["set_read_only"] = False
    assert component.charger_control_read_only_now() is False


def test_switch_off_releases_and_persists():
    """Turning the switch off releases, and is saved so a restart keeps it off."""
    storage = FakeStorage()
    component = FakeComponent([FakeCharger("a")], storage=storage)
    _plan(component, 0, [("06-01 01:00:00", "06-01 02:00:00")])
    run_async(component.charger_control_tick(_now()))
    run_async(component.charger_control_set_enabled(False))
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [("a", "on", 0), ("a", "release", True)], component.commands
    assert storage.saved[("fake", "control_state")] == {"enabled": False}

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


def test_no_switch_without_storage_settings():
    """A component that passes no storage location has no persisted switch."""
    component = FakeComponent([FakeCharger("a")], storage=FakeStorage())
    component.charger_control_setup("Fake", "charger")
    run_async(component.charger_control_save_enabled())
    run_async(component.charger_control_load_enabled())
    assert component.storage.saved == {}


def test_failed_release_is_retried():
    """A release that raises propagates to the component and is tried again next cycle.

    Nothing is forgotten until the release has gone through, so the retry still knows which
    chargers Predbat was holding. A charger released before the failure is released again.
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
    component.base.set_read_only = True
    try:
        run_async(component.charger_control_tick(_now()))
        raise AssertionError("The failed release should reach the component")
    except OSError:
        pass
    assert component.charger_control_released is None, "A failed release is not recorded as done"
    assert set(component.charger_control_state) == {"a", "b"}, component.charger_control_state

    component.charger_control_release_one = real_release
    run_async(component.charger_control_tick(_now()))
    releases = [command for command in component.commands if command[1] == "release"]
    assert releases == [("a", "release", False), ("a", "release", False), ("b", "release", False)], releases
    assert component.charger_control_state == {} and component.charger_control_released is not None


def test_inactive_does_nothing():
    """Control that was never enabled neither commands nor releases."""
    component = FakeComponent([FakeCharger("a")])
    component.charger_control_active = False
    _plan(component, 0, [])
    component.base.set_read_only = True
    run_async(component.charger_control_tick(_now()))
    component.base.set_read_only = False
    run_async(component.charger_control_tick(_now()))
    assert component.commands == [], component.commands


def run_car_charger_control_tests(my_predbat=None):
    """Run the shared charger control tests. Returns True on failure."""
    print("**** Running car charger control tests ****")
    test_no_plan_sends_nothing()
    test_on_inside_window_off_outside()
    test_empty_plan_stops()
    test_each_charger_follows_its_own_car()
    test_charger_without_a_car_is_left_alone()
    test_disconnected_charger_is_left_alone()
    test_drift_is_reapplied()
    test_read_only_releases_once_and_resumes()
    test_read_only_falls_back_to_the_arg()
    test_switch_off_releases_and_persists()
    test_storage_failures_fail_soft()
    test_no_switch_without_storage_settings()
    test_failed_release_is_retried()
    test_inactive_does_nothing()
    return False
