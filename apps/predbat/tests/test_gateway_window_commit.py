"""Predbat's own window writes, through the schedule write switch, to a simulated Hub."""

import asyncio
import contextlib
import datetime
import inspect
import json
from unittest.mock import MagicMock, patch

import gateway_status_pb2 as pb
import inverter as inverter_module
from config import INVERTER_DEF
from dispatch import InverterDispatch
from inverter import Inverter
from tests.test_gateway_commit import CHARGE_ENABLED, DISCHARGE_ENABLED, DISCHARGE_END, DISCHARGE_START, SLOT_END, SLOT_START, WRITE, clock, inject, make_gateway, make_status


class SimulatedHub:
    """One inverter's slots as the Hub firmware keeps them, answering commands with acks.

    An EMS (ems=True) follows ems_plan_update: while a slot is on it refuses a window with a
    missing end or start == end, and while it is off it only records the window. A refusal
    can be forced per command with refuse.
    """

    def __init__(self, gw, ems=False, charge=(0, 230), discharge=(0, 0), charge_enabled=True, discharge_enabled=False):
        self.gw = gw
        self.ems = ems
        self.slots = {"charge": list(charge), "discharge": list(discharge)}
        self.enabled = {"charge": charge_enabled, "discharge": discharge_enabled}
        self.refuse = set()  # commands refused outright
        gw.publish_command = self.publish
        self.frame()

    def frame(self):
        """Publish a status frame of the Hub's current state, as telemetry would."""
        inv_type = pb.INVERTER_TYPE_GIVENERGY_EMS if self.ems else pb.INVERTER_TYPE_GIVENERGY
        inject(self.gw, make_status(inv_type=inv_type, charge=tuple(self.slots["charge"]), discharge=tuple(self.slots["discharge"]), charge_enabled=self.enabled["charge"], discharge_enabled=self.enabled["discharge"]))

    async def publish(self, command, command_id=None, **kwargs):
        """Record a command and ack it the way the firmware would."""
        self.gw._published.append((command, command_id, kwargs))
        side = "discharge" if command.startswith("set_discharge") else "charge"
        ok, applied = True, None
        if command in self.refuse:
            ok = False
        elif command.endswith("_enable"):
            self.enabled[side] = kwargs["enable"]
            applied = {"enable": kwargs["enable"]}
        else:
            schedule = json.loads(kwargs["schedule_json"])
            start, end = schedule.get("start", self.slots[side][0]), schedule.get("end", self.slots[side][1])
            if self.ems and self.enabled[side] and ("end" not in schedule or start == end):
                ok = False
            else:
                self.slots[side] = [start, end]
                applied = {"start": start, "end": end}
                if self.ems and not self.enabled[side]:
                    applied["recorded"] = True
        ack = {"command_id": f"PBAT{command_id}", "command": command, "ok": ok}
        if ok:
            ack["applied"] = applied
        else:
            ack["error"] = "refused"
        self.gw._process_ack(json.dumps(ack).encode("utf-8"))

    def sent(self):
        """Published (command, payload) pairs."""
        return [(command, kwargs.get("schedule_json", kwargs.get("enable"))) for command, _, kwargs in self.gw._published]


class HubBase:
    """Predbat's side of the boundary: config args, state reads, and service calls routed to the real gateway."""

    def __init__(self, gw):
        self.gateway = gw
        self.args = {
            "charge_start_time": SLOT_START,
            "charge_end_time": SLOT_END,
            "scheduled_charge_enable": CHARGE_ENABLED,
            "discharge_start_time": DISCHARGE_START,
            "discharge_end_time": DISCHARGE_END,
            "scheduled_discharge_enable": DISCHARGE_ENABLED,
            "schedule_write_button": WRITE,
        }
        self.prefix = "predbat"
        self.control_ledger = None
        self.set_read_only = self.set_inverter_notify = False
        self.inverter_clock_skew_start = self.inverter_clock_skew_end = 0
        self.inverter_clock_skew_discharge_start = self.inverter_clock_skew_discharge_end = 0
        self.forecast_minutes = 48 * 60
        self.logs, self.errors, self.services = [], [], []

    def get_arg(self, name, default=None, index=None, indirect=True, **kwargs):
        """Resolve configured entities, or their current state."""
        entity = self.args.get(name)
        if entity is None:
            return default
        return self.gateway.cache.get(entity, default) if indirect else entity

    def get_state_wrapper(self, entity_id=None, **kwargs):
        """Read the gateway's published entity state."""
        return self.gateway.cache.get(entity_id)

    def log(self, message):
        """Keep log lines."""
        self.logs.append(message)

    def record_status(self, message, **kwargs):
        """Keep reported errors."""
        self.errors.append(message)

    def call_service_wrapper(self, service, entity_id=None, **kwargs):
        """Route entity writes to the gateway's handlers, as Home Assistant or loopback does; True like a delivered call."""
        self.services.append((service, entity_id))
        domain, action = service.split("/")
        if domain == "select":
            asyncio.run(self.gateway.select_event(entity_id, kwargs["option"]))
        elif domain == "switch":
            asyncio.run(self.gateway.switch_event(entity_id, action))
        return True


def make_inverter(base):
    """A GWMQTT Inverter with just the state the window methods use."""
    inv = Inverter.__new__(Inverter)
    inv.base, inv.log, inv.id = base, base.log, 0
    inv.dispatch = InverterDispatch(inv)
    inv.inverter_type = "GWMQTT"
    inv.count_register_writes = inv.registers_moved = 0
    inv.inv_write_backoff = False
    inv.write_backoff = {}
    inv.inv_write_max_retry, inv.inv_write_and_poll_sleep = 3, 0.01
    inv.inv_has_charge_enable_time = inv.inv_has_discharge_enable_time = True
    inv.inv_has_ge_inverter_mode = False
    inv.inv_time_button_press = INVERTER_DEF["GWMQTT"]["time_button_press"]
    inv.inv_charge_time_format = INVERTER_DEF["GWMQTT"]["charge_time_format"]
    inv.inv_output_charge_control = "power"
    inv.inv_charge_control_immediate = False
    inv.reserve_percent = 4
    inv.commit_pending, inv.last_committed = {}, {}
    inv.charge_enable_time = True
    inv.sleep = lambda seconds: None
    inv.adjust_idle_time = MagicMock()
    inv.adjust_inverter_mode = MagicMock()
    return inv


@contextlib.contextmanager
def hub_cycle(**hub_kwargs):
    """A gateway, simulated Hub, Predbat base and inverter, with the ack clock under test control."""
    now, clock_patch = clock()

    async def pause():
        """An ack wait moves the clock on a second."""
        now["now"] += 1

    with clock_patch, patch("gateway._commit_pause", pause):
        gw = make_gateway()
        hub = SimulatedHub(gw, **hub_kwargs)
        base = HubBase(gw)
        gw.base = base
        yield make_inverter(base), gw, hub, base, now


def at(value):
    """A datetime for HH:MM:SS on the test day."""
    return datetime.datetime.strptime("2026-10-16 " + value, "%Y-%m-%d %H:%M:%S")


def slot_json(start, end):
    """The schedule_json a complete window is sent as."""
    return json.dumps({"start": start, "end": end})


class TestHubWindowCommit:
    """adjust_charge_window / adjust_force_export on a Hub inverter commit through the schedule write switch."""

    def test_gwmqtt_profile_uses_time_button_press(self):
        """The Hub profile commits its windows with the schedule write switch, like Fox."""
        assert INVERTER_DEF["GWMQTT"]["time_button_press"] is True

    def test_inverter_has_no_gateway_coupling(self):
        """inverter.py writes entities only; nothing in it reaches into the gateway component."""
        source = inspect.getsource(inverter_module)
        assert 'get_component("gateway")' not in source
        assert "write_and_poll_gateway" not in source
        assert "gateway_discharge_window_refused" not in source

    def test_hub_charge_window_commits_via_write_switch(self):
        """Moving an enabled EMS charge window 00:00-02:30 to 02:30-03:05 never sends a start on its own."""
        with hub_cycle(ems=True, charge=(0, 230), charge_enabled=True) as (inv, gw, hub, base, now):
            inv.adjust_charge_window(at("02:30:00"), at("03:05:00"), 60)
            slots = [payload for command, payload in hub.sent() if command == "set_charge_slot"]
            assert slots == [slot_json(230, 305)]
            assert hub.slots["charge"] == [230, 305] and hub.enabled["charge"] is True
            assert inv.last_committed["charge_window"] == ("02:30:00", "03:05:00", True)
            assert not inv.commit_pending.get("charge_window")
            assert (gw.cache[SLOT_START], gw.cache[SLOT_END]) == ("02:30:00", "03:05:00")
            assert base.errors == []

    def test_hub_ems_disabled_charge_records_then_enables(self):
        """A disabled EMS records the new window, then the enable Predbat wrote with it is sent."""
        with hub_cycle(ems=True, charge=(0, 230), charge_enabled=False) as (inv, gw, hub, base, now):
            inv.adjust_charge_window(at("02:30:00"), at("03:05:00"), 60)
            assert hub.sent()[-2:] == [("set_charge_slot", slot_json(230, 305)), ("set_charge_enable", True)]
            assert hub.enabled["charge"] is True
            assert gw.cache[CHARGE_ENABLED] == "on"

    def test_hub_refused_commit_retries_next_cycle(self):
        """A refused window shows the Hub's schedule again, so the next cycle writes and commits it again."""
        with hub_cycle(charge=(0, 230), charge_enabled=True) as (inv, gw, hub, base, now):
            hub.refuse.add("set_charge_slot")
            inv.adjust_charge_window(at("02:30:00"), at("03:05:00"), 60)
            assert hub.slots["charge"] == [0, 230]
            assert gw._staged == {}
            hub.refuse.clear()
            now["now"] += 300  # the next 5 minute cycle
            hub.frame()
            assert gw.cache[SLOT_START] == "00:00:00"
            inv.adjust_charge_window(at("02:30:00"), at("03:05:00"), 65)
            assert hub.slots["charge"] == [230, 305] and hub.enabled["charge"] is True
            assert gw._staged == {}

    def test_hub_stable_window_does_not_press_every_cycle(self):
        """Once committed, an unchanged window neither presses the write switch nor sends anything."""
        with hub_cycle(charge=(0, 230), charge_enabled=True) as (inv, gw, hub, base, now):
            inv.adjust_charge_window(at("02:30:00"), at("03:05:00"), 60)
            published, presses = len(gw._published), base.services.count(("switch/turn_on", WRITE))
            now["now"] += 300
            hub.frame()
            inv.adjust_charge_window(at("02:30:00"), at("03:05:00"), 65)
            assert len(gw._published) == published
            assert base.services.count(("switch/turn_on", WRITE)) == presses

    def test_hub_refused_export_move_turns_export_off(self):
        """A refused move of an armed export window turns export off rather than export on the old one (#5532)."""
        with hub_cycle(discharge=(200, 400), discharge_enabled=True) as (inv, gw, hub, base, now):
            hub.refuse.add("set_discharge_slot")
            inv.adjust_force_export(True, at("02:30:00"), at("03:00:00"))
            assert ("set_discharge_slot", slot_json(230, 300)) in hub.sent()
            assert hub.sent()[-1] == ("set_discharge_enable", False)
            assert hub.enabled["discharge"] is False

    def test_hub_export_window_commits_and_enables(self):
        """An export window is sent as one pair and export enabled after it."""
        with hub_cycle(discharge=(0, 0), discharge_enabled=False) as (inv, gw, hub, base, now):
            inv.adjust_force_export(True, at("17:00:00"), at("19:00:00"))
            assert ("set_discharge_slot", slot_json(1700, 1900)) in hub.sent()
            assert [payload for command, payload in hub.sent() if command == "set_discharge_slot"] == [slot_json(1700, 1900)]
            assert hub.slots["discharge"] == [1700, 1900] and hub.enabled["discharge"] is True

    def test_hub_inactive_export_preparation_never_enables(self):
        """Preparing an export window on a disabled EMS records it and leaves export off."""
        with hub_cycle(ems=True, discharge=(0, 0), discharge_enabled=False) as (inv, gw, hub, base, now):
            inv.adjust_force_export(False, at("17:00:00"), at("19:00:00"))
            assert ("set_discharge_slot", slot_json(1700, 1900)) in hub.sent()
            assert not any(command == "set_discharge_enable" and payload is True for command, payload in hub.sent())
            assert hub.enabled["discharge"] is False
