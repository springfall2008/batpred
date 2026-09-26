# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the OCPP 1.6J virtual charge point
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async
from mock_base import MockBase

from components import COMPONENT_LIST, secret_config_names
from ocpp_charger import (
    CHARGE_POINT_MODEL,
    MSG_CALL,
    MSG_CALLERROR,
    MSG_CALLRESULT,
    OCPPCallError,
    OCPPCharger,
    format_service_data,
    parse_ocpp_time,
    schedule_limit_amps,
)

NOW = datetime(2026, 9, 26, 20, 0, 0, tzinfo=timezone.utc)
POWER = "sensor.wallbox_power"
ENERGY = "sensor.wallbox_energy"
PLUGGED = "sensor.wallbox_status"


class FakeWebSocket:
    """Stands in for the central system: records frames and answers the charger's CALLs."""

    def __init__(self, charger, responses=None):
        """Answer each action with its entry in `responses`, or an empty payload."""
        self.charger = charger
        self.responses = responses or {}
        self.frames = []
        self.closed = False

    async def send_str(self, text):
        """Record a frame; answer a CALL on the next loop turn, as a real server would."""
        frame = json.loads(text)
        self.frames.append(frame)
        if frame[0] == MSG_CALL:
            reply = self.responses.get(frame[2], {})
            if isinstance(reply, tuple):
                answer = [MSG_CALLERROR, frame[1], reply[0], reply[1], {}]
            else:
                answer = [MSG_CALLRESULT, frame[1], reply]
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.charger.handle_frame(json.dumps(answer))))

    def calls(self, action=None):
        """The CALL frames the charger sent, optionally only those for `action`."""
        return [frame for frame in self.frames if frame[0] == MSG_CALL and (action is None or frame[2] == action)]


def make_charger(plugged="Charging", power=0.0, energy=None, read_only=False, service_ok=True, **extra_args):
    """Build a charger against a MockBase whose sensors and service calls the test controls."""
    base = MockBase(set_read_only=read_only, **extra_args)
    base.entities[POWER] = {"state": power}
    base.entities[PLUGGED] = {"state": plugged}
    if energy is not None:
        base.entities[ENERGY] = {"state": energy}
    base.service_calls = []

    def call_service_wrapper(service, **kwargs):
        """Record a Home Assistant service call and report the configured outcome."""
        base.service_calls.append((service, kwargs))
        return service_ok

    base.call_service_wrapper = call_service_wrapper
    charger = OCPPCharger(base, charge_point_id="CP1", password="secret", power_sensor=POWER, energy_sensor=ENERGY if energy is not None else None, plugged_sensor=PLUGGED)
    charger.clock = lambda: NOW
    charger.args.setdefault("ocpp_charger_plugged_response", ["charging", "connected", "locked"])
    return base, charger


async def settle():
    """Let scheduled replies and follow-up tasks run."""
    for _ in range(10):
        await asyncio.sleep(0)


def connect(charger, responses=None):
    """Attach a fake central system to the charger."""
    websocket = FakeWebSocket(charger, responses)
    charger._ws = websocket
    charger.connected = True
    return websocket


def test_registry_entry():
    """The component is registered with its credentials flagged secret."""
    entry = COMPONENT_LIST["ocpp_charger"]
    assert entry["class"] == "ocpp_charger.OCPPCharger"
    assert entry["args"]["charge_point_id"]["config"] == "ocpp_charger_id"
    assert entry["args"]["power_sensor"]["required"] is True
    assert "ocpp_charger_password" in secret_config_names()
    assert "ocpp_charger_id" in secret_config_names()
    assert len(CHARGE_POINT_MODEL) <= 20, "OCPP limits chargePointModel to 20 characters"


def test_parse_ocpp_time():
    """Timestamps parse with Z or an offset, and junk gives None."""
    assert parse_ocpp_time("2026-09-26T20:00:00Z") == NOW
    assert parse_ocpp_time("2026-09-26T21:00:00+01:00") == NOW
    assert parse_ocpp_time("not a time") is None
    assert parse_ocpp_time(None) is None


def test_schedule_limit_relative():
    """A Relative profile runs from the transaction start, period by period, until its duration ends."""
    profile = {"chargingProfileKind": "Relative", "chargingSchedule": {"chargingRateUnit": "A", "duration": 3600, "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 32}, {"startPeriod": 1800, "limit": 0}]}}
    started = NOW - timedelta(minutes=10)
    assert schedule_limit_amps(profile, NOW, started, 230) == 32
    assert schedule_limit_amps(profile, NOW + timedelta(minutes=25), started, 230) == 0
    assert schedule_limit_amps(profile, NOW + timedelta(minutes=55), started, 230) is None


def test_schedule_limit_absolute_and_validity():
    """An Absolute profile starts at startSchedule and respects validFrom/validTo."""
    profile = {"chargingProfileKind": "Absolute", "validTo": "2026-09-26T21:00:00Z", "chargingSchedule": {"startSchedule": "2026-09-26T20:30:00Z", "chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16}]}}
    assert schedule_limit_amps(profile, NOW, None, 230) is None, "before startSchedule"
    assert schedule_limit_amps(profile, NOW + timedelta(minutes=45), None, 230) == 16
    assert schedule_limit_amps(profile, NOW + timedelta(hours=2), None, 230) is None, "after validTo"


def test_schedule_limit_watts_and_recurring():
    """A limit in watts converts with numberPhases (1 when absent); a daily profile recurs."""
    profile = {"chargingProfileKind": "Recurring", "recurrencyKind": "Daily", "chargingSchedule": {"startSchedule": "2026-09-20T19:00:00Z", "chargingRateUnit": "W", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 7360}]}}
    assert schedule_limit_amps(profile, NOW, None, 230) == 32
    profile["chargingSchedule"]["chargingSchedulePeriod"][0]["numberPhases"] = 3
    assert abs(schedule_limit_amps(profile, NOW, None, 230) - 7360 / 690) < 1e-9


def test_format_service_data():
    """Placeholders fill from the data; the service key and plain values are left alone."""
    template = {"service": "number.set_value", "entity_id": "number.wallbox_max_current", "value": "{current}", "note": "{unknown}"}
    assert format_service_data(template, {"current": 16, "power": 3680}) == {"entity_id": "number.wallbox_max_current", "value": "16", "note": "{unknown}"}


def test_unknown_action_is_not_implemented():
    """A CALL with no handler gets a NotImplemented CALLERROR."""
    _, charger = make_charger()
    websocket = connect(charger)
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u1", "UpdateFirmware", {}])))
    assert websocket.frames[-1][:3] == [MSG_CALLERROR, "u1", "NotImplemented"]


def test_configuration_round_trip():
    """ChangeConfiguration stores a value, refuses read-only keys, and GetConfiguration reports it."""
    _, charger = make_charger()
    websocket = connect(charger)
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u1", "ChangeConfiguration", {"key": "MeterValueSampleInterval", "value": "30"}])))
    assert websocket.frames[-1] == [MSG_CALLRESULT, "u1", {"status": "Accepted"}]
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u2", "ChangeConfiguration", {"key": "NumberOfConnectors", "value": "2"}])))
    assert websocket.frames[-1] == [MSG_CALLRESULT, "u2", {"status": "Rejected"}]
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u3", "GetConfiguration", {"key": ["MeterValueSampleInterval", "V2GCapabilities"]}])))
    result = websocket.frames[-1][2]
    assert result["configurationKey"] == [{"key": "MeterValueSampleInterval", "readonly": False, "value": "30"}]
    assert result["unknownKey"] == ["V2GCapabilities"]
    assert charger.config_int("MeterValueSampleInterval") == 30


def test_remote_start_opens_transaction():
    """An accepted RemoteStart installs its TxProfile and opens a transaction at the meter reading."""
    _, charger = make_charger(plugged="Connected")
    charger.register_wh = 1500
    websocket = connect(charger, {"StartTransaction": {"transactionId": 7, "idTagInfo": {"status": "Accepted"}}})
    profile = {"chargingProfileId": 0, "stackLevel": 0, "chargingProfilePurpose": "TxProfile", "chargingProfileKind": "Relative", "chargingSchedule": {"chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 32.0}]}}

    async def scenario():
        """Drive the exchange inside one event loop."""
        await charger.handle_frame(json.dumps([MSG_CALL, "u1", "RemoteStartTransaction", {"idTag": "ffffffffffffff7f", "connectorId": 1, "chargingProfile": profile}]))
        await settle()

    run_async(scenario())
    assert websocket.frames[0] == [MSG_CALLRESULT, "u1", {"status": "Accepted"}]
    start = websocket.calls("StartTransaction")[0][3]
    assert start["idTag"] == "ffffffffffffff7f" and start["meterStart"] == 1500
    assert charger.transaction_id == 7
    assert charger.active_limit_amps() == 32


def test_remote_start_rejected_without_car_or_with_open_transaction():
    """RemoteStart is refused when no car is plugged in or a transaction is already open."""
    _, charger = make_charger(plugged="Available")
    websocket = connect(charger)
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u1", "RemoteStartTransaction", {"idTag": "tag"}])))
    assert websocket.frames[-1] == [MSG_CALLRESULT, "u1", {"status": "Rejected"}]

    _, charger = make_charger(plugged="Connected")
    charger.transaction_id = 3
    websocket = connect(charger)
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u2", "RemoteStartTransaction", {"idTag": "tag"}])))
    assert websocket.frames[-1] == [MSG_CALLRESULT, "u2", {"status": "Rejected"}]


def test_remote_start_not_authorised_closes_transaction():
    """A StartTransaction the central system does not authorise is stopped straight away."""
    _, charger = make_charger(plugged="Connected")
    websocket = connect(charger, {"StartTransaction": {"transactionId": 9, "idTagInfo": {"status": "Invalid"}}})

    async def scenario():
        """Drive the exchange inside one event loop."""
        await charger.handle_frame(json.dumps([MSG_CALL, "u1", "RemoteStartTransaction", {"idTag": "tag"}]))
        await settle()

    run_async(scenario())
    assert websocket.calls("StopTransaction")[0][3]["reason"] == "DeAuthorized"
    assert charger.transaction_id is None


def test_remote_stop():
    """RemoteStop for the open transaction stops it with reason Remote; any other id is refused."""
    _, charger = make_charger(plugged="Connected")
    charger.transaction_id = 5
    charger.tx_started = NOW
    websocket = connect(charger)

    async def scenario():
        """Drive the exchange inside one event loop."""
        await charger.handle_frame(json.dumps([MSG_CALL, "u1", "RemoteStopTransaction", {"transactionId": 6}]))
        await charger.handle_frame(json.dumps([MSG_CALL, "u2", "RemoteStopTransaction", {"transactionId": 5}]))
        await settle()

    run_async(scenario())
    assert websocket.frames[0] == [MSG_CALLRESULT, "u1", {"status": "Rejected"}]
    assert websocket.frames[1] == [MSG_CALLRESULT, "u2", {"status": "Accepted"}]
    assert websocket.calls("StopTransaction")[0][3]["reason"] == "Remote"
    assert charger.transaction_id is None
    assert charger.desired_status(True) == "Finishing"


def test_charging_profiles():
    """A TxProfile needs a transaction, TxProfile beats TxDefaultProfile, the max profile caps, and clear works."""
    _, charger = make_charger()
    websocket = connect(charger)
    tx_profile = {"chargingProfileId": 1, "stackLevel": 1, "chargingProfilePurpose": "TxProfile", "chargingProfileKind": "Relative", "chargingSchedule": {"chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 0}]}}
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "u1", "SetChargingProfile", {"connectorId": 1, "csChargingProfiles": tx_profile}])))
    assert websocket.frames[-1][2] == {"status": "Rejected"}

    charger.transaction_id = 1
    charger.tx_started = NOW
    default_profile = dict(tx_profile, chargingProfileId=2, chargingProfilePurpose="TxDefaultProfile", chargingSchedule={"chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 20}]})
    max_profile = dict(tx_profile, chargingProfileId=3, chargingProfilePurpose="ChargePointMaxProfile", chargingSchedule={"chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16}]})
    for index, profile in enumerate([default_profile, max_profile, tx_profile]):
        run_async(charger.handle_frame(json.dumps([MSG_CALL, "p{}".format(index), "SetChargingProfile", {"connectorId": 1, "csChargingProfiles": profile}])))
        assert websocket.frames[-1][2] == {"status": "Accepted"}
    assert charger.active_limit_amps() == 0, "the TxProfile's 0 A wins over the default"

    run_async(charger.handle_frame(json.dumps([MSG_CALL, "c1", "ClearChargingProfile", {"chargingProfilePurpose": "TxProfile"}])))
    assert websocket.frames[-1][2] == {"status": "Accepted"}
    assert charger.active_limit_amps() == 16, "the default's 20 A is capped by the 16 A max profile"
    run_async(charger.handle_frame(json.dumps([MSG_CALL, "c2", "ClearChargingProfile", {"id": 99}])))
    assert websocket.frames[-1][2] == {"status": "Unknown"}


def test_apply_control_follows_transaction_and_limit():
    """The charger is stopped while waiting for the central system, started inside a transaction, and stopped at 0 A."""
    base, charger = make_charger(plugged="Connected", ocpp_charger_start_service={"service": "number.set_value", "entity_id": "number.wallbox_max_current", "value": "{current}"}, ocpp_charger_stop_service="script.wallbox_pause")

    run_async(charger.apply_control(NOW, True))
    assert base.service_calls == [("script/wallbox_pause", {})]

    charger.transaction_id = 1
    charger.tx_started = NOW
    charger.install_profile({"chargingProfileId": 0, "stackLevel": 0, "chargingProfilePurpose": "TxProfile", "chargingProfileKind": "Relative", "chargingSchedule": {"chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 32}, {"startPeriod": 600, "limit": 0}]}})
    run_async(charger.apply_control(NOW, True))
    assert base.service_calls[-1] == ("number/set_value", {"entity_id": "number.wallbox_max_current", "value": "32"})
    assert charger.charge_enabled is True

    run_async(charger.apply_control(NOW + timedelta(seconds=30), True))
    assert len(base.service_calls) == 2, "no repeat while nothing has changed"

    run_async(charger.apply_control(NOW + timedelta(minutes=11), True))
    assert base.service_calls[-1] == ("script/wallbox_pause", {})
    assert charger.charge_enabled is False


def test_apply_control_ignores_unplugged_idle_and_read_only():
    """Nothing is commanded with no car and no transaction, or in read-only mode."""
    base, charger = make_charger(plugged="Available", ocpp_charger_stop_service="script.wallbox_pause")
    run_async(charger.apply_control(NOW, False))
    assert base.service_calls == []

    base, charger = make_charger(plugged="Connected", read_only=True, ocpp_charger_stop_service="script.wallbox_pause")
    run_async(charger.apply_control(NOW, True))
    assert base.service_calls == []


def test_apply_control_retries_failed_command_after_a_minute():
    """A service call that is not accepted is retried, but not before the retry interval."""
    base, charger = make_charger(plugged="Connected", service_ok=False, ocpp_charger_stop_service="script.wallbox_pause")
    run_async(charger.apply_control(NOW, True))
    run_async(charger.apply_control(NOW + timedelta(seconds=30), True))
    assert len(base.service_calls) == 1
    run_async(charger.apply_control(NOW + timedelta(seconds=61), True))
    assert len(base.service_calls) == 2
    assert charger.charge_enabled is None


def test_meter_from_energy_sensor():
    """The register follows energy increases, treats a fall as a new session, and ignores a rebase jump."""
    base, charger = make_charger(power=7000, energy=1.0)
    charger.register_wh = 10000
    charger.sample_meter(NOW)
    assert charger.register_wh == 10000, "the first reading is only a baseline"
    base.entities[ENERGY]["state"] = 1.5
    charger.sample_meter(NOW)
    assert charger.register_wh == 10500
    base.entities[ENERGY]["state"] = 0.2
    charger.sample_meter(NOW)
    assert abs(charger.register_wh - 10700) < 1e-6, "a reset counter contributes its new reading"
    base.entities[ENERGY]["state"] = 500.0
    charger.sample_meter(NOW)
    assert abs(charger.register_wh - 10700) < 1e-6, "a jump bigger than any real sample is ignored"
    assert charger.power_w == 7000


def test_meter_integrates_power_without_energy_sensor():
    """Without an energy sensor power is integrated, but not across a gap too long to trust."""
    base, charger = make_charger(power=3600)
    charger.sample_meter(NOW)
    charger.sample_meter(NOW + timedelta(seconds=60))
    assert abs(charger.register_wh - 60) < 1e-6
    charger.sample_meter(NOW + timedelta(minutes=30))
    assert abs(charger.register_wh - 60) < 1e-6
    base.entities[POWER]["state"] = -50
    charger.sample_meter(NOW + timedelta(minutes=30, seconds=5))
    assert charger.power_w == 0, "a negative reading is not import"


def test_meter_value_measurands():
    """Only measurands Predbat can read are reported; SoC is included when a SoC sensor is set."""
    base, charger = make_charger(power=1234)
    charger.register_wh = 5678.4
    charger.sample_meter(NOW)
    entry = charger.meter_value(NOW, "Sample.Periodic", "Energy.Active.Import.Register,Power.Active.Import,Frequency,SoC")
    assert entry["timestamp"] == "2026-09-26T20:00:00Z"
    assert [(item["measurand"], item["value"]) for item in entry["sampledValue"]] == [("Energy.Active.Import.Register", "5678"), ("Power.Active.Import", "1234")]
    charger.soc_sensor = "sensor.car_soc"
    base.entities["sensor.car_soc"] = {"state": 55.6}
    charger.sample_meter(NOW)
    entry = charger.meter_value(NOW, "Sample.Periodic", "SoC")
    assert entry["sampledValue"] == [{"value": "56", "context": "Sample.Periodic", "measurand": "SoC", "unit": "Percent"}]


def test_tick_reports_status_and_meter():
    """A tick reports the status, then MeterValues on its interval, with the transaction id inside one."""
    base, charger = make_charger(plugged="Connected", power=0)
    websocket = connect(charger)
    run_async(charger.tick())
    assert websocket.calls("StatusNotification")[-1][3]["status"] == "Preparing"
    assert len(websocket.calls("MeterValues")) == 1
    assert "transactionId" not in websocket.calls("MeterValues")[0][3]

    charger.transaction_id = 4
    charger.tx_started = NOW
    base.entities[POWER]["state"] = 7200
    run_async(charger.tick())
    assert websocket.calls("StatusNotification")[-1][3]["status"] == "Charging"
    assert len(websocket.calls("MeterValues")) == 1, "not due again yet"

    base.entities[POWER]["state"] = 20
    later = NOW + timedelta(seconds=61)
    charger.clock = lambda: later
    run_async(charger.tick())
    assert websocket.calls("StatusNotification")[-1][3]["status"] == "SuspendedEV"
    assert websocket.calls("MeterValues")[-1][3]["transactionId"] == 4


def test_tick_unplug_stops_transaction():
    """Unplugging during a transaction stops it with EVDisconnected and reports Available."""
    base, charger = make_charger(plugged="Connected")
    charger.transaction_id = 8
    charger.tx_started = NOW
    websocket = connect(charger)
    base.entities[PLUGGED]["state"] = "Ready"
    run_async(charger.tick())
    assert websocket.calls("StopTransaction")[0][3]["reason"] == "EVDisconnected"
    assert websocket.calls("StatusNotification")[-1][3]["status"] == "Available"
    assert charger.transaction_id is None


def test_tick_clock_aligned_meter_values():
    """Clock-aligned readings are sent when a ClockAlignedDataInterval boundary is crossed."""
    _, charger = make_charger(plugged="Available")
    charger.configuration["ClockAlignedDataInterval"] = "900"
    charger.configuration["MeterValueSampleInterval"] = "0"
    websocket = connect(charger)
    charger.clock = lambda: NOW - timedelta(seconds=10)
    run_async(charger.tick())
    charger.clock = lambda: NOW + timedelta(seconds=5)
    run_async(charger.tick())
    clock_values = [frame for frame in websocket.calls("MeterValues") if frame[3]["meterValue"][0]["sampledValue"][0]["context"] == "Sample.Clock"]
    assert len(clock_values) == 1


def test_trigger_message():
    """TriggerMessage for a supported message is accepted and sent; others are NotImplemented."""
    _, charger = make_charger(plugged="Connected")
    websocket = connect(charger)

    async def scenario():
        """Drive the exchange inside one event loop."""
        await charger.handle_frame(json.dumps([MSG_CALL, "u1", "TriggerMessage", {"requestedMessage": "MeterValues"}]))
        await charger.handle_frame(json.dumps([MSG_CALL, "u2", "TriggerMessage", {"requestedMessage": "FirmwareStatusNotification"}]))
        await settle()

    run_async(scenario())
    assert websocket.frames[0] == [MSG_CALLRESULT, "u1", {"status": "Accepted"}]
    assert [MSG_CALLRESULT, "u2", {"status": "NotImplemented"}] in websocket.frames
    assert len(websocket.calls("MeterValues")) == 1


def test_call_error_raises():
    """A CALLERROR reply surfaces as OCPPCallError to the caller."""
    _, charger = make_charger()
    connect(charger, {"Heartbeat": ("InternalError", "boom")})

    async def scenario():
        """Drive the exchange inside one event loop."""
        try:
            await charger.call("Heartbeat", {})
        except OCPPCallError as e:
            return str(e)
        return None

    assert run_async(scenario()) == "InternalError: boom"


def test_boot_sets_heartbeat():
    """An accepted BootNotification registers the charger and adopts the server's heartbeat interval."""
    _, charger = make_charger()
    websocket = connect(charger, {"BootNotification": {"status": "Accepted", "interval": 10, "currentTime": "2026-09-26T20:00:00Z"}})
    assert run_async(charger.boot()) is True
    assert charger.config_int("HeartbeatInterval") == 10
    assert websocket.calls("BootNotification")[0][3] == {"chargePointVendor": "Predbat", "chargePointModel": "Predbat Virtual"}


def test_plugged_falls_back_to_car_charging_planned():
    """With no plugged sensor configured, Predbat's car_charging_planned and its responses are used."""
    base, charger = make_charger(car_charging_planned="yes", car_charging_planned_response=["yes", "on"])
    charger.plugged_sensor = None
    charger.args.pop("ocpp_charger_plugged_response", None)
    assert charger.car_plugged() is True
    base.args["car_charging_planned"] = "no"
    assert charger.car_plugged() is False


def test_state_survives_restart():
    """The meter register and an open transaction are saved and restored through storage."""

    class FakeStorage:
        """In-memory storage component."""

        def __init__(self):
            """Start empty."""
            self.data = {}

        async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
            """Keep a copy of the data."""
            self.data[(module, filename)] = json.loads(json.dumps(data))
            return True

        async def load(self, module, filename):
            """Return the saved data, if any."""
            return self.data.get((module, filename))

    class FakeComponents:
        """Serves the storage component."""

        def __init__(self, storage):
            """Hold the storage."""
            self.storage = storage

        def get_component(self, name):
            """Return storage for "storage"."""
            return self.storage if name == "storage" else None

    storage = FakeStorage()
    base, charger = make_charger()
    base.components = FakeComponents(storage)
    charger.register_wh = 4321
    charger.transaction_id = 12
    charger.tx_started = NOW
    charger.id_tag = "tag"
    run_async(charger.save_state())

    _, restored = make_charger()
    restored.base.components = FakeComponents(storage)
    run_async(restored.load_state())
    assert restored.register_wh == 4321
    assert restored.transaction_id == 12
    assert restored.tx_started == NOW


def test_connection_against_local_server():
    """A real websocket connection: Basic auth, the ocpp1.6 subprotocol, boot, then status.

    The server closes the socket as soon as it has the StatusNotification, so this also checks a
    CALL in flight at the close fails at once rather than sitting out its timeout.
    """
    from aiohttp import web

    _, charger = make_charger(plugged="Available")
    received = []
    seen = {}

    async def handler(request):
        """Minimal central system: accept the boot, acknowledge everything, close after the status."""
        seen["path"] = request.path
        seen["auth"] = request.headers.get("Authorization")
        websocket = web.WebSocketResponse(protocols=("ocpp1.6",))
        await websocket.prepare(request)
        async for message in websocket:
            frame = json.loads(message.data)
            received.append(frame)
            if frame[0] != MSG_CALL:
                continue
            reply = {"status": "Accepted", "interval": 10, "currentTime": "2026-09-26T20:00:00Z"} if frame[2] == "BootNotification" else {}
            await websocket.send_str(json.dumps([MSG_CALLRESULT, frame[1], reply]))
            if frame[2] == "StatusNotification":
                charger.api_stop = True
                await websocket.close()
        return websocket

    async def scenario():
        """Drive the exchange inside one event loop."""
        app = web.Application()
        app.router.add_get("/{charge_point}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        charger.url = "ws://127.0.0.1:{}".format(port)
        try:
            await asyncio.wait_for(charger.connection_loop(), timeout=15)
        finally:
            await runner.cleanup()

    run_async(scenario())
    assert seen["path"] == "/CP1"
    assert seen["auth"] == "Basic Q1AxOnNlY3JldA==", "Basic auth is charge point id : password"
    actions = [frame[2] for frame in received if frame[0] == MSG_CALL]
    assert actions[:2] == ["BootNotification", "StatusNotification"]
    assert received[1][3]["status"] == "Available"
    assert charger.connected is False
    assert charger.config_int("HeartbeatInterval") == 10


def test_publish_throttled():
    """The status sensor is written on a change, and otherwise at most once a minute."""
    base, charger = make_charger()
    entity = "sensor.predbat_ocpp_charger_status"
    charger.maybe_publish(NOW)
    assert base.entities[entity]["state"] == "Disconnected"
    del base.entities[entity]
    charger.maybe_publish(NOW + timedelta(seconds=30))
    assert entity not in base.entities, "unchanged within a minute"
    charger.connected = True
    charger.status_sent = "Preparing"
    charger.maybe_publish(NOW + timedelta(seconds=35))
    assert base.entities[entity]["state"] == "Preparing", "a change publishes at once"
    del base.entities[entity]
    charger.maybe_publish(NOW + timedelta(seconds=96))
    assert entity in base.entities, "republished after a minute"


def test_simple_handlers():
    """Each simple CALL gets the fixed answer the charger gives it."""
    _, charger = make_charger()
    websocket = connect(charger)
    expected = {
        "SendLocalList": ({"listVersion": 3, "updateType": "Full"}, {"status": "Accepted"}),
        "GetLocalListVersion": ({}, {"listVersion": 3}),
        "ClearCache": ({}, {"status": "Accepted"}),
        "DataTransfer": ({"vendorId": "x"}, {"status": "UnknownVendorId"}),
        "Reset": ({"type": "Soft"}, {"status": "Rejected"}),
        "ChangeAvailability": ({"connectorId": 1, "type": "Inoperative"}, {"status": "Rejected"}),
        "UnlockConnector": ({"connectorId": 1}, {"status": "NotSupported"}),
        "GetCompositeSchedule": ({"connectorId": 1, "duration": 3600}, {"status": "Rejected"}),
    }
    for index, (action, (payload, answer)) in enumerate(expected.items()):
        run_async(charger.handle_frame(json.dumps([MSG_CALL, "h{}".format(index), action, payload])))
        assert websocket.frames[-1] == [MSG_CALLRESULT, "h{}".format(index), answer], action
    run_async(charger.handle_frame("not json"))
    run_async(charger.handle_frame(json.dumps({"not": "a list"})))
    assert len(websocket.frames) == len(expected), "malformed frames are ignored"


def test_run_requires_configuration():
    """run() refuses to start without an id, password and power sensor."""
    _, charger = make_charger()
    charger.power_sensor = None
    assert run_async(charger.run(0, True)) is False


def test_ocpp_charger(my_predbat=None):
    """
    ======================================================================
    OCPP VIRTUAL CHARGER TEST SUITE
    ======================================================================
    Protocol handling, charging profiles, charger control, metering and persistence.
    """
    print("\n" + "=" * 70)
    print("OCPP VIRTUAL CHARGER TEST SUITE")
    print("=" * 70)

    test_registry_entry()
    test_parse_ocpp_time()
    test_schedule_limit_relative()
    test_schedule_limit_absolute_and_validity()
    test_schedule_limit_watts_and_recurring()
    test_format_service_data()
    test_unknown_action_is_not_implemented()
    test_configuration_round_trip()
    test_remote_start_opens_transaction()
    test_remote_start_rejected_without_car_or_with_open_transaction()
    test_remote_start_not_authorised_closes_transaction()
    test_remote_stop()
    test_charging_profiles()
    test_apply_control_follows_transaction_and_limit()
    test_apply_control_ignores_unplugged_idle_and_read_only()
    test_apply_control_retries_failed_command_after_a_minute()
    test_meter_from_energy_sensor()
    test_meter_integrates_power_without_energy_sensor()
    test_meter_value_measurands()
    test_tick_reports_status_and_meter()
    test_tick_unplug_stops_transaction()
    test_tick_clock_aligned_meter_values()
    test_trigger_message()
    test_call_error_raises()
    test_boot_sets_heartbeat()
    test_plugged_falls_back_to_car_charging_planned()
    test_state_survives_restart()
    test_connection_against_local_server()
    test_publish_throttled()
    test_simple_handlers()
    test_run_requires_configuration()

    print("=" * 70)
    return False
