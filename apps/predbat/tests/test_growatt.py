# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Tests for the Growatt public OpenAPI component."""

# cspell:ignore echarge edischarge

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import predbat  # noqa: F401  (import first - config.py imports THIS_VERSION)
import pytz

from components import COMPONENT_LIST
from config import APPS_SCHEMA, INVERTER_DEF
from growatt import GrowattAPI
from growatt_const import (
    GROWATT_BASE_URL,
    GROWATT_ENDPOINTS,
    GROWATT_SET_ENABLE_CONTROL,
    GROWATT_SET_TIME_SLOTS,
    local_window_to_utc_slots,
)
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_infra import run_async as run_async_local


class MockGrowatt(GrowattAPI):
    """Small Growatt component double without the lifecycle thread."""

    def __init__(self, automatic=False):
        self.prefix = "predbat"
        self.local_tz = pytz.timezone("Europe/London")
        self.log_messages = []
        self.state = {}
        self.published = {}
        self.base = MagicMock()
        self.base.args = {"user_id": "growatt-test-instance"}
        self.base.now_utc = datetime.now(pytz.utc)
        self.base.midnight_utc = self.base.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.initialize(
            key="access-token",
            auth_method="oauth",
            token_expires_at=(datetime.now(pytz.utc) + timedelta(days=1)).isoformat(),
            token_hash="token-hash",
            automatic=automatic,
            api_delay=0,
            min_write_interval=5,
        )

    def log(self, message):
        self.log_messages.append(message)

    def dashboard_item(self, entity, state, attributes, app=None):
        self.published[entity] = {"state": state, "attributes": attributes, "app": app}
        self.state[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        return self.state.get(entity_id, default)

    def set_arg_auto(self, arg, value):
        self.base.args[arg] = value

    def update_success_timestamp(self):
        return None


def _ready_client(automatic=False):
    client = MockGrowatt(automatic=automatic)
    sn = "SPH123"
    client.device_list = [sn]
    client.device_detail[sn] = {
        "deviceSn": sn,
        "model": "SPH 6000TL",
        "nominalPower": 6000,
        "existBattery": True,
        "batteryCapacity": 10000,
        "batteryNominalPower": 5000,
        "dischargeCutOffSOC": 10,
    }
    client.device_values[sn] = {"soc": 50.0}
    client.device_energy[sn] = {"load_today": 8.0, "import_today": 2.0, "export_today": 1.0, "pv_today": 10.0}
    return client, sn


def _schedule():
    return {
        "reserve": 15,
        "charge": {"enable": True, "soc": 80, "power": 2500, "start": "01:00:00", "end": "03:00:00"},
        "export": {"enable": True, "soc": 20, "power": 1000, "start": "17:00:00", "end": "19:00:00"},
    }


def test_growatt_constants_and_utc_conversion():
    assert GROWATT_BASE_URL == "https://opencloud.growatt.com"
    assert GROWATT_ENDPOINTS["dispatch"] == "/oauth2/deviceDispatch"
    london = pytz.timezone("Europe/London")
    winter = london.localize(datetime(2026, 1, 15, 12, 0))
    summer = london.localize(datetime(2026, 7, 15, 12, 0))
    assert local_window_to_utc_slots("01:00:00", "03:00:00", 50, winter) == [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    assert local_window_to_utc_slots("01:00:00", "03:00:00", 50, summer) == [{"startTime": "00:00", "endTime": "02:00", "percentage": 50}]
    assert local_window_to_utc_slots("00:00:00", "00:00:00", 50, summer) == []


def test_growatt_utc_midnight_split():
    eastern = pytz.timezone("US/Eastern")
    winter = eastern.localize(datetime(2026, 1, 15, 12, 0))
    # 18:00-19:00 EST is 23:00-00:00 UTC: no after-midnight portion, so no 00:00-00:00 slot.
    assert local_window_to_utc_slots("18:00:00", "19:00:00", 50, winter) == [{"startTime": "23:00", "endTime": "23:59", "percentage": 50}]
    # 18:00-21:00 EST is 23:00-02:00 UTC, split at UTC midnight.
    assert local_window_to_utc_slots("18:00:00", "21:00:00", -30, winter) == [
        {"startTime": "23:00", "endTime": "23:59", "percentage": -30},
        {"startTime": "00:00", "endTime": "02:00", "percentage": -30},
    ]


def test_growatt_stale_telemetry_withholds_slots():
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    payload = {"soc": 50, "utcTime": "2026-01-15 12:00:00"}
    with patch("growatt.time.time", return_value=1_000_000):
        assert client._apply_telemetry(sn, payload)
        assert len(client.build_time_slots(sn, client.local_schedule[sn])) == 2
    # The cloud keeps serving the same sample: the freshness clock does not advance.
    with patch("growatt.time.time", return_value=1_000_000 + 6 * 60):
        assert client._apply_telemetry(sn, dict(payload))
        assert client.telemetry_stale(sn)
        assert client.build_time_slots(sn, client.local_schedule[sn]) == []
        assert any("telemetry has not updated" in message for message in client.log_messages)
    # A new sample restores both slots.
    with patch("growatt.time.time", return_value=1_000_000 + 7 * 60):
        assert client._apply_telemetry(sn, {"soc": 50, "utcTime": "2026-01-15 12:07:00"})
        assert not client.telemetry_stale(sn)
        assert len(client.build_time_slots(sn, client.local_schedule[sn])) == 2


def test_growatt_device_info_failure_keeps_known_device():
    client, sn = _ready_client()

    async def post(endpoint_key, body=None):
        if endpoint_key == "devices":
            return 0, [{"deviceSn": sn}]
        return -1, None

    client._post = post
    assert run_async_local(client.refresh_static())
    assert client.device_list == [sn]
    # A device never seen usable before is still dropped when its details cannot be read.
    client.device_list = []
    assert not run_async_local(client.refresh_static())
    assert client.device_list == []


def test_growatt_telemetry_signs_and_energy_balance():
    client, sn = _ready_client()
    payload = {
        "soc": 67,
        "batPower": 500,
        "meterPower": 300,
        "ppv": 2100,
        "payLoadPower": 1900,
        "maxChargePower": 4800,
        "maxDischargePower": 5000,
        "etoUserToday": 3.0,
        "etoGridToday": 2.0,
        "epvToday": 10.0,
        "batteryList": [{"echargeToday": 4.0, "edischargeToday": 1.0}],
    }
    assert client._apply_telemetry(sn, payload)
    assert client.device_values[sn]["battery_power"] == -500
    assert client.device_values[sn]["grid_power"] == -300
    assert client.device_values[sn]["pv_power"] == 2100
    # PV + import + discharge - export - charge = 8 kWh load.
    assert client.device_energy[sn]["load_today"] == 8.0
    assert client.battery_rate_max(sn) == 4800


def test_growatt_time_slots_use_power_percentages_and_soc_gates():
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    slots = client.build_time_slots(sn, client.local_schedule[sn])
    assert sorted(slot["percentage"] for slot in slots) == [-20, 50]

    client.device_values[sn]["soc"] = 80
    slots = client.build_time_slots(sn, client.local_schedule[sn])
    assert [slot["percentage"] for slot in slots] == [-20]
    assert any("public API has no writable SoC limit" in message for message in client.log_messages)

    client.device_values[sn]["soc"] = 15
    slots = client.build_time_slots(sn, client.local_schedule[sn])
    assert [slot["percentage"] for slot in slots] == [50]


def test_growatt_control_entities_round_trip():
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    run_async_local(client.publish_schedule_settings_ha(sn))
    read_back = run_async_local(client.get_schedule_settings_ha(sn))
    assert read_back == _schedule()
    assert client.published["number.predbat_growatt_sph123_battery_schedule_reserve"]["attributes"]["min"] == 10


def test_growatt_apply_schedule_enables_control_then_deduplicates():
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    calls = []

    async def dispatch(serial, set_type, value):
        calls.append((serial, set_type, value))
        return True

    client.dispatch = dispatch
    # Avoid a real five-second wait while still proving the enable-control call is issued.
    with patch("growatt.asyncio.sleep", new=AsyncMock()):
        assert run_async_local(client.apply_schedule(sn))
        assert [call[1] for call in calls] == [GROWATT_SET_ENABLE_CONTROL, GROWATT_SET_TIME_SLOTS]
        # Same payload is accepted as a no-op, without a third API call.
        assert run_async_local(client.apply_schedule(sn))
        assert len(calls) == 2


def test_growatt_automatic_config_and_publish():
    client, sn = _ready_client(automatic=True)
    client.device_values[sn].update({"battery_power": 100, "grid_power": -50, "pv_power": 700, "load_power": 550})
    run_async_local(client.publish_data())
    run_async_local(client.automatic_config())
    assert client.base.args["inverter_type"] == ["GrowattCloud"]
    assert client.base.args["soc_percent"] == ["sensor.predbat_growatt_sph123_soc"]
    assert client.base.args["grid_power_invert"] == [False]
    assert client.base.args["charge_limit"] == ["number.predbat_growatt_sph123_battery_schedule_charge_soc"]
    assert "sensor.predbat_growatt_sph123_battery_capacity" in client.published


def test_growatt_registration_and_schema():
    entry = COMPONENT_LIST["growatt"]
    assert entry["class"] == "growatt.GrowattAPI"
    assert entry["required_or"] == ["key", "token_hash"]
    assert entry["args"]["key"]["secret"] is True
    assert entry["args"]["token_hash"]["secret"] is True
    assert entry["args"]["control_enable"]["default"] is True
    for key, kind in {
        "growatt_key": "string",
        "growatt_token_hash": "string",
        "growatt_inverter_sn": "string|string_list",
        "growatt_automatic": "boolean",
        "growatt_control_enable": "boolean",
        "growatt_base_url": "string",
        "growatt_battery_rate_max": "float",
        "growatt_min_write_interval": "integer",
    }.items():
        assert APPS_SCHEMA[key]["type"] == kind


def test_growatt_inverter_definition_is_complete():
    entry = INVERTER_DEF["GrowattCloud"]
    reference = set(INVERTER_DEF["HanchuCloud"])
    assert not (reference - set(entry))
    assert entry["output_charge_control"] == "power"
    assert entry["has_target_soc"] is True
    assert entry["has_reserve_soc"] is True
    assert entry["support_charge_freeze"] is False
    assert entry["support_discharge_freeze"] is False
    assert entry["write_and_poll_sleep"] == 5


def test_growatt_discovery_matches_definition_and_automatic_config():
    client, _ = _ready_client(automatic=True)
    records = validated_inverters(client.build_discovery())
    captured = capture_automatic_config(client)
    assert len(records) == 1
    assert_definition_complete(records[0], GrowattAPI.WRITE_AND_POLL_SLEEP)
    assert_record_agrees(records[0], captured)
    assert_record_binds_nothing_extra(records[0], captured)


def test_growatt_request_ids_are_unique_32_character_hex():
    first = GrowattAPI.request_id()
    second = GrowattAPI.request_id()
    assert len(first) == 32
    assert int(first, 16) >= 0
    assert first != second


def test_growatt_refresh_threshold_fits_two_hour_tokens():
    client = MockGrowatt()
    with patch("growatt.time.time", return_value=1_000_000):
        client.token_expires_at = 1_000_000 + 11 * 60
        assert client._token_needs_refresh() is False
        client.token_expires_at = 1_000_000 + 9 * 60
        assert client._token_needs_refresh() is True


def run_growatt_tests(my_predbat):
    """Run the Growatt component tests in Predbat's custom test harness."""
    failed = False
    tests = [
        ("constants_utc", test_growatt_constants_and_utc_conversion),
        ("utc_midnight_split", test_growatt_utc_midnight_split),
        ("stale_telemetry", test_growatt_stale_telemetry_withholds_slots),
        ("device_info_failure", test_growatt_device_info_failure_keeps_known_device),
        ("telemetry", test_growatt_telemetry_signs_and_energy_balance),
        ("slots_soc_gates", test_growatt_time_slots_use_power_percentages_and_soc_gates),
        ("control_round_trip", test_growatt_control_entities_round_trip),
        ("write_dedup", test_growatt_apply_schedule_enables_control_then_deduplicates),
        ("automatic_publish", test_growatt_automatic_config_and_publish),
        ("registration", test_growatt_registration_and_schema),
        ("inverter_definition", test_growatt_inverter_definition_is_complete),
        ("discovery_contract", test_growatt_discovery_matches_definition_and_automatic_config),
        ("request_id", test_growatt_request_ids_are_unique_32_character_hex),
        ("refresh_threshold", test_growatt_refresh_threshold_fits_two_hour_tokens),
    ]
    for name, test in tests:
        try:
            test()
        except Exception as error:
            print("  EXCEPTION in growatt.{}: {}".format(name, error))
            import traceback

            traceback.print_exc()
            failed = True
    return failed
