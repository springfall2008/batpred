"""Hub slot windows stage in the gateway and are sent when the schedule write switch is pressed."""

import asyncio
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
