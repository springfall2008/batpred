"""Hub slot windows stage in the gateway and are sent when the schedule write switch is pressed."""

import asyncio
import functools
import json
from unittest.mock import MagicMock, patch

import pytz

import gateway_status_pb2 as pb
from gateway import GatewayMQTT

SLOT_START = "select.predbat_gateway_456789_charge_slot1_start"
SLOT_END = "select.predbat_gateway_456789_charge_slot1_end"
DISCHARGE_START = "select.predbat_gateway_456789_discharge_slot1_start"
DISCHARGE_END = "select.predbat_gateway_456789_discharge_slot1_end"
CHARGE_ENABLED = "switch.predbat_gateway_456789_charge_enabled"
DISCHARGE_ENABLED = "switch.predbat_gateway_456789_discharge_enabled"
SERIAL = "CE123456789"
SUFFIX = "456789"


def make_gateway(subscribed=True, acks_seen=True):
    """A gateway that records published commands and keeps entity state in gw.cache."""
    gw = GatewayMQTT.__new__(GatewayMQTT)
    gw.base = MagicMock()
    gw.log = MagicMock()
    gw.prefix = "predbat"
    gw.args = {}
    gw.local_tz = pytz.timezone("Europe/London")
    gw._suffix_to_serial = {SUFFIX: SERIAL}
    gw._mqtt_connected = True
    gw._command_id = 0
    gw._pending_commands = {}
    gw._ack_subscribed = subscribed
    gw._acks_seen = acks_seen
    gw._ack_subscribe_warned = False
    gw._last_status = None
    gw._last_read_only = None
    gw._read_only_mismatch_logged = False
    gw._loop = None
    gw._staged = {}
    gw._commit_inflight = set()
    gw._commit_tasks = set()
    gw._published = []  # (command, command_id, kwargs)
    gw.cache = {}  # entity_id -> state, as the generic write loop reads it back
    gw.attrs = {}  # entity_id -> attributes of the last publish

    async def fake_publish_command(command, command_id=None, **kwargs):
        gw._published.append((command, command_id, kwargs))

    def fake_dashboard_item(entity_id, state=None, attributes=None, app=None):
        gw.cache[entity_id] = state
        gw.attrs[entity_id] = attributes

    gw.publish_command = fake_publish_command
    gw.dashboard_item = fake_dashboard_item
    gw.get_state_wrapper = lambda entity_id=None, **kwargs: gw.cache.get(entity_id)
    return gw


def make_status(inv_type=pb.INVERTER_TYPE_GIVENERGY, charge=(130, 430), discharge=(1600, 1900), charge_enabled=True, discharge_enabled=False):
    """A one-inverter status frame for SERIAL with the given schedule and enables."""
    status = pb.GatewayStatus()
    status.device_id = "pbgw_abc123"
    status.firmware = "1.0.38"
    status.timestamp = 1741789200
    status.schema_version = 1
    inv = status.inverters.add()
    inv.type = inv_type
    inv.serial = SERIAL
    inv.primary = True
    inv.connected = True
    inv.active = True
    inv.battery.soc_percent = 50
    inv.battery.capacity_wh = 9500
    inv.battery.rate_max_w = 5000
    inv.control.charge_enabled = charge_enabled
    inv.control.discharge_enabled = discharge_enabled
    inv.control.charge_rate_w = 3000
    inv.control.discharge_rate_w = 3000
    inv.control.target_soc = 100
    inv.schedule.charge_start, inv.schedule.charge_end = charge
    inv.schedule.discharge_start, inv.schedule.discharge_end = discharge
    return status


def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


def clock():
    """A controllable gateway._monotonic clock."""
    now = {"now": 1000.0}
    return now, patch("gateway._monotonic", lambda: now["now"])


def inject(gw, status):
    """Feed a status frame through telemetry injection, as _process_telemetry does."""
    gw._last_status = status
    gw._inject_entities(status)


class TestGatewayStaging:
    """Slot endpoints, and an enable written alongside them, stay in the gateway until the schedule is written."""

    def test_slot_select_stages_without_publishing(self):
        """A slot start write publishes nothing, reads back at once and pairs with the current end."""
        gw = make_gateway()
        gw.cache[SLOT_END] = "04:30:00"
        run(gw.select_event(SLOT_START, "02:30:00"))
        assert gw._published == []
        assert gw.cache[SLOT_START] == "02:30:00"
        staged = gw._staged[(SUFFIX, "charge")]
        assert (staged["start"], staged["end"], staged["enable"]) == (230, 430, None)

    def test_telemetry_does_not_override_staged_slot(self):
        """A status frame keeps showing the staged start; the side with nothing staged follows telemetry."""
        gw = make_gateway()
        gw.cache[SLOT_END] = "04:30:00"
        now, patcher = clock()
        with patcher:
            run(gw.select_event(SLOT_START, "02:30:00"))
            inject(gw, make_status(charge=(130, 430)))
        assert gw.cache[SLOT_START] == "02:30:00"
        assert gw.cache[SLOT_END] == "04:30:00"
        assert gw.cache[DISCHARGE_START] == "16:00:00"

    def test_enable_staged_only_while_pair_staged(self):
        """An enable joins a staged pair for its side; with nothing staged it is sent as before."""
        gw = make_gateway()
        gw.cache[SLOT_END] = "04:30:00"
        gw.cache[CHARGE_ENABLED] = "off"
        run(gw.select_event(SLOT_START, "02:30:00"))
        run(gw.switch_event(CHARGE_ENABLED, "turn_on"))
        assert gw._published == []
        assert gw.cache[CHARGE_ENABLED] == "on"
        assert gw._staged[(SUFFIX, "charge")]["enable"] is True

        run(gw.switch_event(DISCHARGE_ENABLED, "turn_on"))
        assert [(command, kwargs) for command, _, kwargs in gw._published] == [("set_discharge_enable", {"enable": True, "serial": SERIAL})]

    def test_staged_value_expires_to_telemetry(self):
        """A staged value nobody writes to the Hub reverts to telemetry after ten minutes."""
        gw = make_gateway()
        gw.cache[SLOT_END] = "04:30:00"
        now, patcher = clock()
        with patcher:
            run(gw.select_event(SLOT_START, "02:30:00"))
            now["now"] += 601
            inject(gw, make_status(charge=(130, 430)))
        assert gw.cache[SLOT_START] == "01:30:00"
        assert gw._staged == {}

    def test_staging_applies_to_coordinator_and_direct(self):
        """An EMS stages too, and its staged select keeps the same-day capability attribute."""
        gw = make_gateway()
        gw._discovery = {SERIAL: {"type": pb.INVERTER_TYPE_GIVENERGY_EMS}}
        gw.cache[DISCHARGE_END] = "19:00:00"
        run(gw.select_event(DISCHARGE_START, "17:00:00"))
        assert gw._published == []
        assert gw.cache[DISCHARGE_START] == "17:00:00"
        assert gw.attrs[DISCHARGE_START]["can_span_midnight"] is False

        direct = make_gateway()
        direct.cache[SLOT_START] = "01:30:00"
        run(direct.select_event(SLOT_END, "05:00:00"))
        assert direct._published == []
        assert (direct._staged[(SUFFIX, "charge")]["start"], direct._staged[(SUFFIX, "charge")]["end"]) == (130, 500)


WRITE = "switch.predbat_gateway_456789_schedule_write"


def slot_json(start, end):
    """The schedule_json a complete window is sent as."""
    return json.dumps({"start": start, "end": end})


def applies(command, kwargs):
    """A Hub that applies every command."""
    if command.endswith("_slot"):
        return True, json.loads(kwargs["schedule_json"])
    return True, {"enable": kwargs["enable"]}


class FakeHub:
    """Answers published commands each time the commit waits for an ack, as the Hub would.

    responder(command, kwargs) returns (ok, applied), or None for no answer.
    """

    def __init__(self, gw, now, responder=applies):
        self.gw = gw
        self.now = now
        self.responder = responder
        self.answered = 0
        self.waits = 0
        self.during = None  # coroutine function run once, on the first wait

    async def pause(self):
        """One ack wait: time moves on a second and the Hub answers what it has been sent."""
        self.waits += 1
        self.now["now"] += 1
        if self.during is not None:
            during, self.during = self.during, None
            await during()
        while self.answered < len(self.gw._published):
            command, command_id, kwargs = self.gw._published[self.answered]
            self.answered += 1
            reply = self.responder(command, kwargs)
            if reply is None:
                continue
            ok, applied = reply
            ack = {"command_id": f"PBAT{command_id}", "command": command, "ok": ok}
            if ok:
                ack["applied"] = applied
            else:
                ack["error"] = "would_disable_charge_zero_length_slot"
            self.gw._process_ack(json.dumps(ack).encode("utf-8"))


def sent(gw):
    """Published (command, payload) pairs, without command ids or serial."""
    result = []
    for command, _, kwargs in gw._published:
        payload = kwargs.get("schedule_json", kwargs.get("enable"))
        result.append((command, payload))
    return result


def hub_test(method):
    """Stop the patches setup_hub() started once the test ends; run_gateway_tests calls no tearDown."""

    @functools.wraps(method)
    def wrapper(self):
        """Run the test, then stop its patches."""
        try:
            return method(self)
        finally:
            self.tearDown()

    return wrapper


class TestGatewayCommit:
    """Pressing the schedule write switch sends each staged window to the Hub, on the gateway's own loop."""

    def setup_hub(self, status=None, responder=applies, **kwargs):
        """A gateway with telemetry injected, a controllable clock and a fake Hub answering acks."""
        gw = make_gateway(**kwargs)
        now, clock_patch = clock()
        hub = FakeHub(gw, now, responder)
        patches = [clock_patch, patch("gateway._commit_pause", hub.pause)]
        for active in patches:
            active.start()
        self._patches = patches
        inject(gw, status or make_status())
        return gw, hub

    def tearDown(self):
        """Stop the clock and pause patches."""
        for active in getattr(self, "_patches", []):
            active.stop()
        self._patches = []

    def stage(self, gw, side, start, end, enable=None):
        """Write a window (and optionally its enable) the way Predbat does, before pressing."""
        run(gw.select_event(f"select.predbat_gateway_456789_{side}_slot1_start", start))
        run(gw.select_event(f"select.predbat_gateway_456789_{side}_slot1_end", end))
        if enable is not None:
            run(gw.switch_event(f"switch.predbat_gateway_456789_{side}_enabled", "turn_on" if enable else "turn_off"))

    def press(self, gw):
        """Press the schedule write switch as press_and_poll_button does."""
        run(gw.switch_event(WRITE, "turn_on"))

    @hub_test
    def test_write_switch_publishes_full_pair_and_resets(self):
        """One press sends the complete window once, clears the staging and leaves the switch off."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305))]
        assert gw._published[0][2]["serial"] == SERIAL
        assert gw.cache[WRITE] == "off"
        assert gw._staged == {}
        assert (gw.cache[SLOT_START], gw.cache[SLOT_END]) == ("02:30:00", "03:05:00")

    @hub_test
    def test_ems_enabled_full_pair_avoids_zero_length_refusal(self):
        """Moving 00:00-02:30 to 02:30-03:05 on an enabled EMS never sends the start on its own."""

        def ems(command, kwargs):
            """An enabled EMS refuses a window with a missing or equal end (ems_plan_update)."""
            schedule = json.loads(kwargs["schedule_json"])
            if "end" not in schedule or schedule["start"] == schedule["end"]:
                return False, None
            return True, schedule

        gw, hub = self.setup_hub(make_status(inv_type=pb.INVERTER_TYPE_GIVENERGY_EMS, charge=(0, 230), charge_enabled=True), responder=ems)
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305))]
        assert gw._staged == {}
        assert gw.cache[SLOT_START] == "02:30:00"

    @hub_test
    def test_ems_disabled_recorded_then_enable(self):
        """A disabled EMS records the window; the staged enable then goes out after it."""

        def ems(command, kwargs):
            """A disabled EMS records a complete window, and applies the enable."""
            if command == "set_charge_slot":
                return True, {**json.loads(kwargs["schedule_json"]), "recorded": True}
            return True, {"enable": kwargs["enable"]}

        gw, hub = self.setup_hub(make_status(inv_type=pb.INVERTER_TYPE_GIVENERGY_EMS, charge_enabled=False), responder=ems)
        self.stage(gw, "charge", "02:30:00", "03:05:00", enable=True)
        assert gw._published == []
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305)), ("set_charge_enable", True)]
        assert gw._staged == {}
        assert gw.cache[CHARGE_ENABLED] == "on"

    @hub_test
    def test_ems_inactive_export_preparation_never_enables(self):
        """An export window prepared while export is off is recorded and never enabled."""

        def ems(command, kwargs):
            """A disabled EMS records the export window."""
            return True, {**json.loads(kwargs["schedule_json"]), "recorded": True}

        gw, hub = self.setup_hub(make_status(inv_type=pb.INVERTER_TYPE_GIVENERGY_EMS, discharge_enabled=False), responder=ems)
        self.stage(gw, "discharge", "17:00:00", "19:00:00")
        self.press(gw)
        assert sent(gw) == [("set_discharge_slot", slot_json(1700, 1900))]
        assert gw._staged == {}

    @hub_test
    def test_refused_pair_drops_staging(self):
        """A refused window is not re-sent; the selects show the Hub's schedule again for the next cycle."""
        gw, hub = self.setup_hub(responder=lambda command, kwargs: (False, None))
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305))]
        assert gw._staged == {}
        assert (gw.cache[SLOT_START], gw.cache[SLOT_END]) == ("01:30:00", "04:30:00")
        assert any("Warn:" in str(call.args[0]) and "charge window" in str(call.args[0]) for call in gw.log.call_args_list)

    @hub_test
    def test_missing_ack_resends_up_to_three_times(self):
        """An unanswered window is re-sent once per ack window, three sends in all, then given up."""
        gw, hub = self.setup_hub(responder=lambda command, kwargs: None)
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305))] * 3
        assert gw._staged == {}
        assert gw.cache[SLOT_START] == "01:30:00"

    @hub_test
    def test_refused_export_move_while_armed_disables_export(self):
        """A refused export move with the old export window armed turns export off (#5532)."""
        gw, hub = self.setup_hub(make_status(discharge_enabled=True), responder=lambda command, kwargs: (False, None) if command.endswith("_slot") else (True, {"enable": kwargs["enable"]}))
        self.stage(gw, "discharge", "17:00:00", "19:00:00")
        self.press(gw)
        assert sent(gw) == [("set_discharge_slot", slot_json(1700, 1900)), ("set_discharge_enable", False)]

    @hub_test
    def test_unanswered_export_move_leaves_export_running(self):
        """Only a refusal turns export off: an unanswered window may well have applied (#5532)."""
        gw, hub = self.setup_hub(make_status(discharge_enabled=True), responder=lambda command, kwargs: None)
        self.stage(gw, "discharge", "17:00:00", "19:00:00")
        self.press(gw)
        assert sent(gw) == [("set_discharge_slot", slot_json(1700, 1900))] * 3
        assert gw._staged == {}

    @hub_test
    def test_refused_charge_move_does_not_disable_charge(self):
        """A refused charge move leaves the charge enable alone, as before."""
        gw, hub = self.setup_hub(make_status(charge_enabled=True), responder=lambda command, kwargs: (False, None))
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305))]

    @hub_test
    def test_staged_disable_is_sent_before_the_pair(self):
        """A disable held with a window goes out first, so a refused window cannot leave the side enabled."""
        gw, hub = self.setup_hub(make_status(charge_enabled=True), responder=lambda command, kwargs: (False, None) if command.endswith("_slot") else (True, {"enable": kwargs["enable"]}))
        self.stage(gw, "charge", "00:00:00", "00:00:00", enable=False)
        self.press(gw)
        assert sent(gw)[0] == ("set_charge_enable", False)
        assert ("set_charge_slot", slot_json(0, 0)) in sent(gw)

    @hub_test
    def test_second_press_during_commit_is_not_lost(self):
        """A press while one side is committing commits the other side, and a newer window follows the current one."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")

        async def meanwhile():
            """Predbat writes the export window and a later charge end, then presses again."""
            await gw.select_event(DISCHARGE_START, "17:00:00")
            await gw.select_event(DISCHARGE_END, "19:00:00")
            await gw.select_event(SLOT_END, "03:30:00")
            await gw.switch_event(WRITE, "turn_on")

        hub.during = meanwhile
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305)), ("set_discharge_slot", slot_json(1700, 1900)), ("set_charge_slot", slot_json(230, 330))]
        assert gw._staged == {}

    @hub_test
    def test_staged_enable_is_sent_although_telemetry_still_reports_on(self):
        """Moving a future window disables, then re-enables with the window: the stale "on" frame must not skip the enable."""
        gw, hub = self.setup_hub(make_status(charge_enabled=True))
        run(gw.switch_event(CHARGE_ENABLED, "turn_off"))  # disable_charge_window(), sent at once
        self.stage(gw, "charge", "02:30:00", "03:05:00", enable=True)
        self.press(gw)
        assert sent(gw) == [("set_charge_enable", False), ("set_charge_slot", slot_json(230, 305)), ("set_charge_enable", True)]
        assert gw.cache[CHARGE_ENABLED] == "on"

    def logged(self, gw, text, level="Warn:"):
        """Whether a log line at this level contains text."""
        return any(str(call.args[0]).startswith(level) and text in str(call.args[0]) for call in gw.log.call_args_list)

    @hub_test
    def test_disconnected_commit_is_not_reported_written(self):
        """With the Hub disconnected nothing is sent, so the window is not reported written and the selects revert."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        gw._mqtt_connected = False
        self.press(gw)
        assert gw._published == []
        assert gw._staged == {}
        assert gw.cache[SLOT_START] == "01:30:00"
        assert self.logged(gw, "not written to the Hub")
        assert not self.logged(gw, "written to the Hub", level="Info:")

    @hub_test
    def test_first_commit_after_reconnect_waits_for_ack(self):
        """Before any ack on a new connection the commit still waits for one, so a refusal is seen and the export fail-safe runs."""
        gw, hub = self.setup_hub(make_status(discharge_enabled=True), responder=lambda command, kwargs: (False, None) if command.endswith("_slot") else (True, {"enable": kwargs["enable"]}), acks_seen=False)
        self.stage(gw, "discharge", "17:00:00", "19:00:00")
        self.press(gw)
        assert sent(gw) == [("set_discharge_slot", slot_json(1700, 1900)), ("set_discharge_enable", False)]

    @hub_test
    def test_hub_that_never_acks_is_reported_published(self):
        """A Hub that has sent no ack on this connection gets one ack window; then the window counts as published, not written."""
        gw, hub = self.setup_hub(responder=lambda command, kwargs: None, acks_seen=False)
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305))]
        assert gw._staged == {}
        assert self.logged(gw, "published to the Hub", level="Info:")
        assert not self.logged(gw, "not written")

    @hub_test
    def test_export_fail_safe_runs_when_telemetry_is_unknown(self):
        """A refused export move with the inverter missing from the last status frame still turns export off."""
        gw, hub = self.setup_hub(make_status(discharge_enabled=True), responder=lambda command, kwargs: (False, None) if command.endswith("_slot") else (True, {"enable": kwargs["enable"]}))
        self.stage(gw, "discharge", "17:00:00", "19:00:00")
        empty = pb.GatewayStatus()
        empty.device_id = "pbgw_abc123"
        gw._last_status = empty
        self.press(gw)
        assert sent(gw)[-1] == ("set_discharge_enable", False)

    @hub_test
    def test_clear_staged_keeps_a_newer_entry(self):
        """Clearing by identity leaves a window staged after the one being cleared."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        sent_entry = gw._staged[(SUFFIX, "charge")]
        run(gw.select_event(SLOT_END, "03:30:00"))
        assert gw._clear_staged(SERIAL, "charge", sent_entry) is False
        assert gw._staged[(SUFFIX, "charge")]["end"] == 330
        assert gw._clear_staged(SERIAL, "charge", gw._staged[(SUFFIX, "charge")]) is True
        assert gw._staged == {}

    @hub_test
    def test_newer_window_staged_during_a_failed_commit_is_sent(self):
        """A window written while a refused one was in flight is sent, not dropped with the failure."""
        refusals = {"left": 1}

        def refuse_once(command, kwargs):
            """Refuse the first window, apply everything after."""
            if command.endswith("_slot") and refusals["left"]:
                refusals["left"] -= 1
                return False, None
            return applies(command, kwargs)

        gw, hub = self.setup_hub(responder=refuse_once)
        self.stage(gw, "charge", "02:30:00", "03:05:00")

        async def meanwhile():
            """A later end is written before the Hub answers."""
            await gw.select_event(SLOT_END, "03:30:00")

        hub.during = meanwhile
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305)), ("set_charge_slot", slot_json(230, 330))]
        assert gw._staged == {}
        assert gw.cache[SLOT_END] == "03:30:00"

    @hub_test
    def test_press_on_a_closed_loop_does_not_raise(self):
        """After the gateway loop has closed a press is handled inline and fails cleanly instead of raising."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        loop = asyncio.new_event_loop()
        loop.close()
        gw._loop = loop
        gw._mqtt_connected = False
        self.press(gw)
        assert gw._published == []
        assert gw._staged == {}

    @hub_test
    def test_final_drops_staging(self):
        """Stopping the component drops staged windows so nothing waits on a loop that is going away."""
        gw, hub = self.setup_hub()
        gw._mqtt_task = None
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        run(gw.final())
        assert gw._staged == {}
        assert gw._commit_inflight == set()

    @hub_test
    def test_unknown_other_endpoint_warns_when_staged(self):
        """Staging one endpoint while the other select reads nothing usable warns straight away."""
        gw, hub = self.setup_hub()
        gw.cache[SLOT_END] = "unknown"
        run(gw.select_event(SLOT_START, "02:30:00"))
        assert self.logged(gw, "charge slot end")

    @hub_test
    def test_staged_entry_read_has_no_side_effects(self):
        """Reading a staged entry never drops or logs; an expired entry is pruned once, by the next status frame."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        hub.now["now"] += 601
        assert gw._staged_entry(SUFFIX, "charge") is not None
        assert gw._staged_entry(SUFFIX, "charge") is not None
        assert not self.logged(gw, "never written")
        inject(gw, make_status())
        assert gw._staged == {}
        assert sum("never written" in str(call.args[0]) for call in gw.log.call_args_list) == 1

    @hub_test
    def test_refused_export_move_after_an_enable_ack_disables_export(self):
        """Export armed by an ack the last status frame predates is still turned off when the next move is refused."""
        refuse = {"slot": False}

        def hub_reply(command, kwargs):
            """Apply everything until told to refuse windows."""
            if command.endswith("_slot") and refuse["slot"]:
                return False, None
            return applies(command, kwargs)

        gw, hub = self.setup_hub(make_status(discharge_enabled=False), responder=hub_reply)
        self.stage(gw, "discharge", "17:00:00", "19:00:00", enable=True)
        self.press(gw)
        assert sent(gw)[-1] == ("set_discharge_enable", True)
        refuse["slot"] = True
        self.stage(gw, "discharge", "18:00:00", "20:00:00")  # no status frame in between: it still says export off
        self.press(gw)
        assert sent(gw)[-1] == ("set_discharge_enable", False)

    @hub_test
    def test_earlier_ack_does_not_mask_a_refused_retry(self):
        """A pair applied in an earlier cycle, then changed outside Predbat, is refused on retry: export is not enabled onto it."""
        refuse = {"slot": False}

        def hub_reply(command, kwargs):
            """Apply everything until told to refuse windows."""
            if command.endswith("_slot") and refuse["slot"]:
                return False, None
            return applies(command, kwargs)

        gw, hub = self.setup_hub(make_status(discharge_enabled=False), responder=hub_reply)
        self.stage(gw, "discharge", "17:00:00", "19:00:00", enable=True)
        self.press(gw)
        before = len(gw._published)
        hub.now["now"] += 300  # next cycle; the Hub's schedule was changed and export turned off meanwhile
        inject(gw, make_status(discharge=(1600, 1900), discharge_enabled=False))
        refuse["slot"] = True
        self.stage(gw, "discharge", "17:00:00", "19:00:00", enable=True)
        self.press(gw)
        retry = sent(gw)[before:]
        assert retry[0] == ("set_discharge_slot", slot_json(1700, 1900))
        assert ("set_discharge_enable", True) not in retry

    @hub_test
    def test_late_unit_refusal_of_an_export_window_disables_export(self):
        """One unit applies the export window, another refuses it after the commit has finished: export is turned off."""
        gw, hub = self.setup_hub(make_status(discharge_enabled=True))
        self.stage(gw, "discharge", "17:00:00", "19:00:00")
        self.press(gw)
        command, command_id, kwargs = gw._published[0]
        assert command == "set_discharge_slot"
        gw._process_ack(json.dumps({"command_id": f"PBAT{command_id}", "command": command, "ok": False, "error": "modbus_write_failed"}).encode("utf-8"))
        assert sent(gw)[-1] == ("set_discharge_enable", False)

    @hub_test
    def test_publish_failure_keeps_a_newer_staged_window(self):
        """A publish that raises after a newer window was staged fails only the old window; the newer one is sent."""
        gw, hub = self.setup_hub()
        self.stage(gw, "charge", "02:30:00", "03:05:00")
        calls = {"n": 0}
        original = gw.publish_command

        async def flaky_publish(command, command_id=None, **kwargs):
            """Stage a later end, then fail the first publish."""
            calls["n"] += 1
            if calls["n"] == 1:
                await gw.select_event(SLOT_END, "03:30:00")
                raise OSError("synthetic MQTT failure")
            await original(command, command_id=command_id, **kwargs)

        gw.publish_command = flaky_publish
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 330))]
        assert gw._staged == {}

    @hub_test
    def test_press_without_staging_publishes_nothing(self):
        """A press with nothing staged (e.g. after a target SOC change) sends nothing."""
        gw, hub = self.setup_hub()
        self.press(gw)
        assert gw._published == []
        assert gw.cache[WRITE] == "off"

    @hub_test
    def test_commit_without_acks_publishes_once_and_clears_staging(self):
        """Without an ack subscription the window and enable are published once each and staging is dropped."""
        gw, hub = self.setup_hub(make_status(charge_enabled=False), subscribed=False)
        self.stage(gw, "charge", "02:30:00", "03:05:00", enable=True)
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(230, 305)), ("set_charge_enable", True)]
        assert hub.waits == 0
        assert gw._staged == {}

    @hub_test
    def test_direct_battery_uses_same_commit(self):
        """A directly controlled AIO commits the same way, and the applied ack updates its selects."""
        gw, hub = self.setup_hub(make_status(inv_type=pb.INVERTER_TYPE_GIVENERGY, charge_enabled=True))
        self.stage(gw, "charge", "23:30:00", "05:30:00")
        self.press(gw)
        assert sent(gw) == [("set_charge_slot", slot_json(2330, 530))]
        assert (gw.cache[SLOT_START], gw.cache[SLOT_END]) == ("23:30:00", "05:30:00")

    @hub_test
    def test_schedule_write_switch_published_per_control_target(self):
        """Telemetry publishes the schedule write switch, always off, beside the slot selects."""
        gw, hub = self.setup_hub()
        assert gw.cache[WRITE] == "off"
        assert gw.attrs[WRITE]["friendly_name"] == "Schedule Write"

    @hub_test
    def test_write_switch_handler_does_not_block(self):
        """With the gateway's loop running elsewhere, the switch handler returns before the Hub answers."""
        import threading
        import time

        gw, hub = self.setup_hub(responder=lambda command, kwargs: None)
        for active in self._patches[1:]:
            active.stop()  # real ack waits on the gateway's own loop
        self._patches = self._patches[:1]
        # ...but polled faster than the real 0.5s, so the commit sees the ack without the test sitting out a whole tick
        poll_patch = patch("gateway._COMMIT_POLL_SECONDS", 0.01)
        poll_patch.start()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        gw._loop = loop
        try:
            self.stage(gw, "charge", "02:30:00", "03:05:00")
            started = time.time()
            self.press(gw)
            assert time.time() - started < 1
            for _ in range(200):
                if gw._published:
                    break
                time.sleep(0.01)
            assert sent(gw) == [("set_charge_slot", slot_json(230, 305))]
            assert (SUFFIX, "charge") in gw._commit_inflight
            command_id = gw._published[0][1]
            gw._process_ack(json.dumps({"command_id": f"PBAT{command_id}", "command": "set_charge_slot", "ok": True, "applied": {"start": 230, "end": 305}}).encode("utf-8"))
            for _ in range(300):
                if not gw._staged:
                    break
                time.sleep(0.01)
            assert gw._staged == {}
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()
            poll_patch.stop()
