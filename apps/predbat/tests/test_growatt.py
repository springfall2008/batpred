# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Tests for the Growatt public OpenAPI component."""

# cspell:ignore echarge edischarge

import contextlib
import io
import json
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import predbat  # noqa: F401  (import first - config.py imports THIS_VERSION)
import pytz

from components import COMPONENT_LIST
from config import APPS_SCHEMA, INVERTER_DEF
from growatt import (
    GrowattAPI,
    build_cli_test_slots,
    classify_cli_reading,
    cli_slot_end,
    growatt_write_test,
    judge_cli_window,
    load_cli_snapshot,
    merge_growatt_cli_args,
    restore_growatt_snapshot,
    save_cli_snapshot,
    test_growatt_api as growatt_cli_run,
)
from growatt_const import (
    GROWATT_BASE_URL,
    GROWATT_ENDPOINTS,
    GROWATT_SET_ENABLE_CONTROL,
    GROWATT_SET_TIME_SLOTS,
    GROWATT_CONTROL_ON,
    GROWATT_UNUSED_SLOT,
    GROWATT_READ_ATTEMPTS,
    GROWATT_WRITE_ATTEMPTS,
    GROWATT_WRITE_BACKOFF_MAX,
    GROWATT_WRITE_CONFIRMED,
    GROWATT_WRITE_MISMATCH,
    GROWATT_WRITE_UNKNOWN,
    dispatch_slot_payload,
    local_window_to_utc_slots,
    parse_dispatch_slots,
)
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_infra import run_async as run_async_local


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self):
        """Start empty."""
        self.data = {}

    async def load(self, module, name):
        """Return a saved entry, or None as the real Storage component does when absent."""
        return self.data.get((module, name))

    async def save(self, module, name, payload, format="yaml", expiry=None, indent=None):
        """Record a copy taken through JSON, as a real save serialises the payload."""
        self.data[(module, name)] = json.loads(json.dumps(payload))


class MockGrowatt(GrowattAPI):
    """Small Growatt component double without the lifecycle thread."""

    def __init__(self, automatic=False, store=None):
        self._store = store
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

    async def _sleep(self, seconds):
        """Record the pacing and back-off waits instead of sleeping."""
        self.slept = getattr(self, "slept", []) + [seconds]

    @property
    def storage(self):
        """Return the fake store, or None for no Storage component."""
        return self._store


def _ready_client(automatic=False, store=None):
    client = MockGrowatt(automatic=automatic, store=store)
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
        "charge": {"enable": True, "soc": 80, "power": 2500, "start": "01:00:00", "end": "03:00:00"},
        "export": {"enable": True, "soc": 20, "power": 1000, "start": "17:00:00", "end": "19:00:00"},
    }


def test_growatt_constants_and_utc_conversion():
    assert GROWATT_BASE_URL == "https://opencloud.growatt.com/prod-api"
    assert GROWATT_ENDPOINTS["dispatch"] == "/oauth2/deviceDispatch"
    london = pytz.timezone("Europe/London")
    winter = london.localize(datetime(2026, 1, 15, 12, 0))
    summer = london.localize(datetime(2026, 7, 15, 12, 0))
    assert local_window_to_utc_slots("01:00:00", "03:00:00", 50, winter) == [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    assert local_window_to_utc_slots("01:00:00", "03:00:00", 50, summer) == [{"startTime": "00:00", "endTime": "02:00", "percentage": 50}]
    assert local_window_to_utc_slots("00:00:00", "00:00:00", 50, summer) == []


class _RecordingResponse:
    """A 200 response with an empty successful Growatt answer."""

    status = 200

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, *exc):
        """Leave the response context."""
        return False

    async def json(self):
        """Return a successful, empty Growatt answer."""
        return {"code": 0, "data": []}


class _RecordingSession:
    """Stands in for the HTTP session _request() opens, recording each URL it posts to."""

    def __init__(self, urls):
        """Record into the given list."""
        self.urls = urls

    async def __aenter__(self):
        """Enter the session context."""
        return self

    async def __aexit__(self, *exc):
        """Leave the session context."""
        return False

    def post(self, url, headers=None, json=None):
        """Record the URL and answer with a successful response."""
        self.urls.append(url)
        return _RecordingResponse()


def test_growatt_request_url_includes_prod_api():
    """Requests built from the default base reach /prod-api/oauth2/..., with or without a trailing slash."""
    registry_default = COMPONENT_LIST["growatt"]["args"]["base_url"]["default"]
    assert registry_default == GROWATT_BASE_URL
    for base_url in (None, registry_default, GROWATT_BASE_URL + "/"):
        client = MockGrowatt()
        if base_url is not None:
            client.initialize(key="access-token", auth_method="oauth", token_expires_at=(datetime.now(pytz.utc) + timedelta(days=1)).isoformat(), base_url=base_url, api_delay=0)
        urls = []
        client.http_session = lambda **kwargs: _RecordingSession(urls)
        code, _ = run_async_local(client._request("device_info", {"deviceSn": "SPH123"}))
        assert code == 0
        assert urls == ["https://opencloud.growatt.com/prod-api/oauth2/getDeviceInfo"], (base_url, urls)
        assert urls[0].startswith("https://opencloud.growatt.com/prod-api/oauth2/")


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
    # Growatt uploads about every five minutes, so one late upload (6 minutes) is not stale...
    with patch("growatt.time.time", return_value=1_000_000 + 6 * 60):
        assert client._apply_telemetry(sn, dict(payload))
        assert not client.telemetry_stale(sn)
        assert len(client.build_time_slots(sn, client.local_schedule[sn])) == 2
    # ...but two missed uploads (13 minutes of the same sample) are, and both slots are withheld.
    with patch("growatt.time.time", return_value=1_000_000 + 13 * 60):
        assert client._apply_telemetry(sn, dict(payload))
        assert client.telemetry_stale(sn)
        assert client.build_time_slots(sn, client.local_schedule[sn]) == []
        assert any("telemetry has not updated" in message for message in client.log_messages)
    # A new sample restores both slots.
    with patch("growatt.time.time", return_value=1_000_000 + 14 * 60):
        assert client._apply_telemetry(sn, {"soc": 50, "utcTime": "2026-01-15 12:14:00"})
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

    # At the charge target the charge slot is kept at 0% power, so the battery holds
    client.device_values[sn]["soc"] = 80
    slots = client.build_time_slots(sn, client.local_schedule[sn])
    assert sorted(slot["percentage"] for slot in slots) == [-20, 0]
    charge = [slot for slot in slots if slot["percentage"] == 0][0]
    assert (charge["startTime"], charge["endTime"]) in (("01:00", "03:00"), ("00:00", "02:00"))
    assert any("holding the charge slot at 0% power" in message for message in client.log_messages)

    # At the export floor the export slot is still withheld
    client.device_values[sn]["soc"] = 15
    slots = client.build_time_slots(sn, client.local_schedule[sn])
    assert [slot["percentage"] for slot in slots] == [50]
    assert any("public API has no writable SoC limit" in message for message in client.log_messages)


def test_growatt_charge_hold_hysteresis():
    """The 0% hold lasts until SoC falls more than one point below target, then full power resumes."""
    client, sn = _ready_client()
    schedule = _schedule()
    schedule["export"]["enable"] = False
    client.local_schedule[sn] = schedule

    def charge_percent(soc):
        """Build the table at this SoC and return the charge slot's percentage."""
        client.device_values[sn]["soc"] = soc
        return [slot["percentage"] for slot in client.build_time_slots(sn, schedule)]

    assert charge_percent(79.5) == [50]
    assert charge_percent(80) == [0]
    # Inside the band the hold stays, so the table (and the device) is not rewritten
    assert charge_percent(79.5) == [0]
    assert charge_percent(79) == [0]
    assert charge_percent(78.5) == [50]
    assert any("resuming the charge" in message for message in client.log_messages)
    # Back below target without having held: charging, not holding
    assert charge_percent(79.5) == [50]
    assert charge_percent(81) == [0]
    # A disabled window forgets the hold, so the next window starts by charging
    schedule["charge"]["enable"] = False
    assert charge_percent(79.5) == []
    schedule["charge"]["enable"] = True
    assert charge_percent(79.5) == [50]


def test_growatt_hold_slot_is_written_once():
    """Reaching the target rewrites the table once, to the 0% slot; holding there sends nothing more."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.control_enabled.add(sn)
    calls = _recording_dispatch(client)
    assert run_async_local(client.apply_schedule(sn))
    client.device_values[sn]["soc"] = 80
    client.last_write_time[sn] = 0
    assert run_async_local(client.apply_schedule(sn))
    assert sorted(slot["percentage"] for slot in calls[-1][2]) == [-20, 0]
    client.device_values[sn]["soc"] = 79.4
    client.last_write_time[sn] = 0
    assert run_async_local(client.apply_schedule(sn))
    assert len(calls) == 2


def test_growatt_control_entities_round_trip():
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    run_async_local(client.publish_schedule_settings_ha(sn))
    read_back = run_async_local(client.get_schedule_settings_ha(sn))
    assert read_back == _schedule()
    # No reserve control: GrowattCloud does not claim has_reserve_soc
    assert not any("reserve" in entity for entity in client.published)


def test_growatt_apply_schedule_enables_control_then_deduplicates():
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    calls = _recording_dispatch(client)
    assert run_async_local(client.apply_schedule(sn))
    assert [call[1] for call in calls] == [GROWATT_SET_ENABLE_CONTROL, GROWATT_SET_TIME_SLOTS]
    # Each write is read back before the next request, paced at the five-second limit
    assert calls.reads == [GROWATT_SET_ENABLE_CONTROL, GROWATT_SET_TIME_SLOTS]
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
    # The inverter's own self-consumption ignores any reserve outside a slot, so none is claimed
    assert entry["has_reserve_soc"] is False
    assert entry["support_charge_freeze"] is False
    assert entry["support_discharge_freeze"] is False
    # One dispatch per five seconds per device is documented; 6 leaves a second of margin
    assert entry["write_and_poll_sleep"] == 6


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


class _DeviceCalls(list):
    """The (serial, set_type, value) writes a simulated device received, with its state and reads."""


def _recording_dispatch(client, result=True, applies=None, enable=0):
    """Put a simulated device behind client.dispatch/read_dispatch; return the list of writes it received.

    result is each write's answer, and applies (default: result) whether the device takes it, so
    a write can answer failure yet apply, as a live SPH did. Reads return what the device holds:
    the slot table as all 16 entries, unused ones 00:00-00:00, or the enable_control value. An
    empty list is refused like the live device (WRONG_PARAMETER_VALUE) and changes nothing.
    """
    calls = _DeviceCalls()
    calls.reads = []
    calls.enable = enable
    calls.table = [dict(GROWATT_UNUSED_SLOT) for _ in range(16)]
    applies = result if applies is None else applies

    async def dispatch(serial, set_type, value):
        """Record a write, and apply it unless the device is set to ignore writes or it is an empty list."""
        calls.append((serial, set_type, value))
        if set_type == GROWATT_SET_TIME_SLOTS and value == []:
            return False
        if applies:
            if set_type == GROWATT_SET_ENABLE_CONTROL:
                calls.enable = value
            else:
                active = [dict(slot) for slot in value if slot["startTime"] != slot["endTime"]]
                calls.table = active + [dict(GROWATT_UNUSED_SLOT) for _ in range(16 - len(active))]
        return result

    async def read_dispatch(serial, set_type):
        """Answer a read with what the device holds."""
        calls.reads.append(set_type)
        if set_type == GROWATT_SET_ENABLE_CONTROL:
            return True, calls.enable
        return True, [dict(slot) for slot in calls.table]

    client.dispatch = dispatch
    client.read_dispatch = read_dispatch
    return calls


def test_growatt_empty_table_payload():
    """An empty table is sent as 16 unused slots, which the device accepts; a real table is sent as it is."""
    assert dispatch_slot_payload([]) == [dict(GROWATT_UNUSED_SLOT) for _ in range(16)]
    assert dispatch_slot_payload(None) == [dict(GROWATT_UNUSED_SLOT) for _ in range(16)]
    # The device's own empty read-back normalises to [] and is sent back the same way
    assert dispatch_slot_payload([dict(GROWATT_UNUSED_SLOT) for _ in range(16)]) == [dict(GROWATT_UNUSED_SLOT) for _ in range(16)]
    table = [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    assert dispatch_slot_payload(table) == table
    # And the placeholders read back as an empty table
    assert parse_dispatch_slots(dispatch_slot_payload([])) == []


def test_growatt_write_answering_failure_but_applied_is_confirmed():
    """A write that answers failure but reads back as written counts as done, without a second write."""
    client, sn = _ready_client()
    calls = _recording_dispatch(client, result=False, applies=True)
    table = [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    assert run_async_local(client.write_slots_verified(sn, table))
    assert len(calls) == 1
    assert any("but the read-back shows it applied" in message for message in client.log_messages)


def test_growatt_write_never_applied_is_retried_then_fails():
    """A write the device never takes is retried with back-off, and the known table becomes unknown."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.control_enabled.add(sn)
    client.applied_slots[sn] = []
    calls = _recording_dispatch(client, result=True, applies=False)
    assert not run_async_local(client.apply_schedule(sn))
    assert len(calls) == GROWATT_WRITE_ATTEMPTS
    assert len(calls.reads) == GROWATT_WRITE_ATTEMPTS
    # A 5 s back-off between the attempts, on top of the pacing waits
    assert 5 in client.slept
    assert sn not in client.applied_slots
    # The device read back a different table: a mismatch, so enable_control is sent again first next time
    assert client.last_write_outcome == GROWATT_WRITE_MISMATCH
    assert sn not in client.control_enabled
    assert sn not in client.unverified_slots
    assert client.write_failures[sn] == 1
    assert any("not confirmed after" in message and "retrying in 1 minute(s)" in message for message in client.log_messages)


def test_growatt_reads_retry_while_the_device_is_busy():
    """READ_DEVICE_PARAM_FAIL is retried with back-off, so one busy answer is not taken as the result."""
    client, sn = _ready_client()
    answers = [(False, None), (True, 1)]
    client.read_dispatch = AsyncMock(side_effect=answers)
    assert run_async_local(client.read_dispatch_retry(sn, GROWATT_SET_ENABLE_CONTROL)) == (True, 1)
    assert client.read_dispatch.await_count == 2
    # A device that keeps failing is given up on after GROWATT_READ_ATTEMPTS, not retried indefinitely
    client.read_dispatch = AsyncMock(return_value=(False, None))
    assert run_async_local(client.read_dispatch_retry(sn, GROWATT_SET_ENABLE_CONTROL)) == (False, None)
    assert client.read_dispatch.await_count == GROWATT_READ_ATTEMPTS == 2


def test_growatt_dispatch_is_not_resent_after_a_transport_failure():
    """A dispatch write is sent once and confirmed by reading back; a read is retried on transport failure."""
    import aiohttp

    for endpoint, expected_posts in (("dispatch", 1), ("device_info", 3)):
        client = MockGrowatt()
        posts = []

        class _FailingSession(_RecordingSession):
            """A session whose every post times out."""

            def post(self, url, headers=None, json=None):
                """Record the URL, then fail like a timeout."""
                self.urls.append(url)
                raise aiohttp.ClientError("timed out")

        client.http_session = lambda **kwargs: _FailingSession(posts)
        with patch("growatt.asyncio.sleep", new=AsyncMock()):
            code, _ = run_async_local(client._request(endpoint, {"deviceSn": "SPH123"}))
        assert code == -1
        assert len(posts) == expected_posts, (endpoint, posts)


def _idle_schedule():
    """A schedule with both windows disabled, so Predbat wants an empty slot table."""
    schedule = _schedule()
    schedule["charge"]["enable"] = False
    schedule["export"]["enable"] = False
    return schedule


def test_growatt_unknown_table_is_written_even_when_empty():
    """With no known table, an empty one is still written, clearing slots a previous run left behind."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _idle_schedule()
    calls = _recording_dispatch(client)
    assert run_async_local(client.apply_schedule(sn))
    # Growatt refuses an empty list, so the empty table goes out as 16 unused slots
    assert calls == [(sn, GROWATT_SET_ENABLE_CONTROL, GROWATT_CONTROL_ON), (sn, GROWATT_SET_TIME_SLOTS, [dict(GROWATT_UNUSED_SLOT) for _ in range(16)])]
    # Now the empty table is known, so it is not sent again
    client.last_write_time[sn] = 0
    assert run_async_local(client.apply_schedule(sn))
    assert len(calls) == 2


def test_growatt_control_state_survives_restart():
    """The last written table and who holds control are restored, so a restart neither rewrites nor forgets them."""
    store = FakeStorage()
    first, sn = _ready_client(store=store)
    first.local_schedule[sn] = _schedule()
    first.control_active.add(sn)
    _recording_dispatch(first)
    with patch("growatt.asyncio.sleep", new=AsyncMock()):
        assert run_async_local(first.apply_schedule(sn))
    written = first.applied_slots[sn]
    assert written

    second, _ = _ready_client(store=store)
    run_async_local(second.restore_state())
    assert second._state_restored
    assert second.applied_slots[sn] == written
    assert second.control_active == {sn}
    assert second.control_claimed == {sn}
    # The table is known again, so the same plan sends nothing
    second.local_schedule[sn] = _schedule()
    calls = _recording_dispatch(second)
    assert run_async_local(second.apply_schedule(sn))
    assert calls == []
    # A changed plan re-sends enable_control once in the new process, then the table
    second.local_schedule[sn] = _idle_schedule()
    with patch("growatt.asyncio.sleep", new=AsyncMock()):
        assert run_async_local(second.apply_schedule(sn))
    assert [call[1] for call in calls] == [GROWATT_SET_ENABLE_CONTROL, GROWATT_SET_TIME_SLOTS]


def test_growatt_restore_without_storage_is_a_no_op():
    """No Storage component means nothing to restore, and it is not retried every run."""
    client, _ = _ready_client()
    run_async_local(client.restore_state())
    assert client._state_restored
    assert client.applied_slots == {}


def test_growatt_parse_dispatch_slots_shapes():
    """The read-back parser accepts the plausible envelopes and refuses to guess on anything else."""
    slot = {"startTime": "01:00", "endTime": "03:00", "percentage": 50}
    expected = [slot]
    assert parse_dispatch_slots([slot]) == expected
    assert parse_dispatch_slots(json.dumps([slot])) == expected
    assert parse_dispatch_slots({"setType": GROWATT_SET_TIME_SLOTS, "value": json.dumps([slot])}) == expected
    assert parse_dispatch_slots({"result": {"timeSlots": [slot]}}) == expected
    # Seconds are dropped to match the write format
    assert parse_dispatch_slots([{"startTime": "01:00:00", "endTime": "03:00:00", "percentage": 50}]) == expected
    # An empty table is only believed under "value"
    assert parse_dispatch_slots({"value": []}) == []
    assert parse_dispatch_slots([]) == []
    assert parse_dispatch_slots({"errors": []}) is None
    for unknown in (None, "", "not json", 5, {"value": "1"}, [{"start": "01:00"}]):
        assert parse_dispatch_slots(unknown) is None, unknown


def test_growatt_readback_live_empty_table():
    """A live SPH answers with all 16 slots, unused ones as 00:00-00:00 at 0%: that is an empty table, equal to Predbat's []."""
    unused = {"percentage": 0, "startTime": "00:00", "endTime": "00:00"}
    live_empty = [dict(unused) for _ in range(16)]
    assert parse_dispatch_slots(live_empty) == []
    used = [{"percentage": 50, "startTime": "01:00", "endTime": "03:00"}] + [dict(unused) for _ in range(15)]
    assert parse_dispatch_slots(used) == [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]

    # Read back as empty while Predbat also wants nothing: no mismatch and no rewrite
    client, sn = _ready_client()
    client.local_schedule[sn] = _idle_schedule()
    client.applied_slots[sn] = []
    client.read_dispatch = AsyncMock(return_value=(True, live_empty))
    assert run_async_local(client.refresh_readback(sn))
    assert not any("differ from the" in message for message in client.log_messages)
    calls = _recording_dispatch(client)
    client.last_write_time[sn] = 0
    assert run_async_local(client.apply_schedule(sn))
    assert calls == []


def test_growatt_readback_adopts_device_table():
    """A read-back replaces the known table, so a table changed on the device is rewritten on the next apply."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.applied_slots[sn] = client.build_time_slots(sn, client.local_schedule[sn])
    client.control_enabled.add(sn)
    device_table = [{"startTime": "10:00", "endTime": "11:00", "percentage": 100}]
    client.read_dispatch = AsyncMock(return_value=(True, {"value": device_table}))
    assert run_async_local(client.refresh_readback(sn))
    assert client.applied_slots[sn] == device_table
    assert any("differ from the" in message for message in client.log_messages)
    run_async_local(client.publish_data())
    sensor = client.published["sensor.predbat_growatt_sph123_dispatch_slots"]
    assert sensor["state"] == 1
    assert sensor["attributes"]["slots"] == device_table
    assert not client.tier_expired("readback:{}".format(sn), 10)

    calls = _recording_dispatch(client)
    client.last_write_time[sn] = 0
    assert run_async_local(client.apply_schedule(sn))
    assert [call[1] for call in calls] == [GROWATT_SET_TIME_SLOTS]
    assert client.applied_slots[sn] == calls[0][2]


def test_growatt_readback_unknown_answer_keeps_written_table():
    """An answer the parser does not understand, or a failed read, leaves the last written table in place."""
    client, sn = _ready_client()
    written = [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    client.applied_slots[sn] = list(written)
    client.read_dispatch = AsyncMock(return_value=(True, {"something": "else"}))
    assert not run_async_local(client.refresh_readback(sn))
    assert client.applied_slots[sn] == written
    assert any("read-back was not understood" in message for message in client.log_messages)
    assert sn not in client.readback_slots

    client.last_write_time[sn] = 0
    client.read_dispatch = AsyncMock(return_value=(False, None))
    assert not run_async_local(client.refresh_readback(sn))
    assert client.applied_slots[sn] == written


def test_growatt_readback_waits_for_dispatch_pacing():
    """A read straight after a write waits for the next run instead of breaking the per-device limit."""
    client, sn = _ready_client()
    client.read_dispatch = AsyncMock(return_value=(True, {"value": []}))
    client.last_write_time[sn] = time.monotonic()
    assert not run_async_local(client.refresh_readback(sn))
    client.read_dispatch.assert_not_called()
    assert client.tier_expired("readback:{}".format(sn), 10)


def test_growatt_release_control():
    """Releasing clears the slot table once - enable_control 0 is never sent - and backs off a refused release."""
    store = FakeStorage()
    client, sn = _ready_client(store=store)
    client.control_claimed.add(sn)
    client.control_enabled.add(sn)
    held = [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    client.applied_slots[sn] = list(held)

    # The device refuses and still holds its slot, so the release is not confirmed
    calls = _recording_dispatch(client, result=False, enable=1)
    calls.table = [dict(slot) for slot in held] + [dict(GROWATT_UNUSED_SLOT) for _ in range(15)]
    assert not run_async_local(client.release_control(sn))
    assert client.control_claimed == {sn}
    # A refused release waits GROWATT_RELEASE_RETRY minutes before trying again
    attempts = len(calls)
    client.last_write_time[sn] = 0
    assert not run_async_local(client.release_control(sn))
    assert len(calls) == attempts

    client._tier_refreshed.pop("release:{}".format(sn))
    client.last_write_time[sn] = 0
    calls = _recording_dispatch(client)
    assert run_async_local(client.release_control(sn))
    assert calls == [(sn, GROWATT_SET_TIME_SLOTS, [dict(GROWATT_UNUSED_SLOT) for _ in range(16)])]
    assert not any(call[1] == GROWATT_SET_ENABLE_CONTROL for call in calls)
    assert client.control_claimed == set()
    assert client.control_enabled == set()
    # The device is now known to hold no slots
    assert client.applied_slots[sn] == []
    assert store.data[("growatt", "control")]["control_claimed"] == []
    # Nothing left to release
    assert run_async_local(client.release_control(sn))
    assert len(calls) == 1


def test_growatt_run_releases_control_when_read_only():
    """run() releases control while read-only, and resumes writing (re-enabling first) once it is turned off."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.control_active.add(sn)
    client.control_claimed.add(sn)
    client.control_enabled.add(sn)
    client.refresh_discovery = MagicMock()
    now = time.time()
    client._tier_refreshed = {"static": now, "live": now, "readback:{}".format(sn): now}
    run_async_local(client.publish_schedule_settings_ha(sn))
    calls = _recording_dispatch(client)

    client.state["switch.predbat_set_read_only"] = "on"
    assert run_async_local(client.run(60, False))
    # Released by clearing the slot table
    assert calls == [(sn, GROWATT_SET_TIME_SLOTS, [dict(GROWATT_UNUSED_SLOT) for _ in range(16)])]
    assert client.control_claimed == set()
    # Still read-only: nothing further is sent
    client.last_write_time[sn] = 0
    assert run_async_local(client.run(60, False))
    assert len(calls) == 1

    client.state["switch.predbat_set_read_only"] = "off"
    client.last_write_time[sn] = 0
    with patch("growatt.asyncio.sleep", new=AsyncMock()):
        assert run_async_local(client.run(60, False))
    assert [call[1] for call in calls[1:]] == [GROWATT_SET_ENABLE_CONTROL, GROWATT_SET_TIME_SLOTS]
    assert client.control_claimed == {sn}


def test_growatt_monitoring_only_releases_claimed_control():
    """growatt_control_enable false releases control Predbat still holds from before, as read-only does."""
    client, sn = _ready_client()
    client.control_enable = False
    client.control_claimed.add(sn)
    client.refresh_discovery = MagicMock()
    now = time.time()
    client._tier_refreshed = {"static": now, "live": now, "readback:{}".format(sn): now}
    calls = _recording_dispatch(client)
    assert run_async_local(client.run(60, False))
    assert calls == [(sn, GROWATT_SET_TIME_SLOTS, [dict(GROWATT_UNUSED_SLOT) for _ in range(16)])]


def test_growatt_zero_capacity_leaves_soc_max_unset():
    """A device reporting batteryCapacity 0 (as a live SPH does) binds no soc_max, so Predbat estimates the size."""
    client, sn = _ready_client(automatic=True)
    client.device_detail[sn]["batteryCapacity"] = 0
    client.device_detail[sn]["batteryList"] = [{"batteryCapacity": 0, "batteryNominalPower": 3680}]
    client.device_values[sn].update({"battery_power": 0, "grid_power": 0, "pv_power": 0, "load_power": 0})
    run_async_local(client.publish_data())
    run_async_local(client.automatic_config())
    assert "soc_max" not in client.base.args
    assert "sensor.predbat_growatt_sph123_battery_capacity" not in client.published
    assert any("soc_max is left unset" in message for message in client.log_messages)
    # The discovery record carries no capacity either, and still agrees with automatic_config()
    records = validated_inverters(client.build_discovery())
    assert "soc_max" not in records[0].get("ratings", {})
    assert "soc_max" not in records[0].get("entities", {})
    captured = capture_automatic_config(client)
    assert_record_agrees(records[0], captured)
    assert_record_binds_nothing_extra(records[0], captured)


def test_growatt_cli_merge_args():
    """apps.yaml fills only what the command line left out, and a parsed timestamp becomes ISO text."""
    config = {
        "growatt_key": "file-token",
        "growatt_token_hash": "file-hash",
        "growatt_token_expires_at": datetime(2026, 11, 11, 20, 0, tzinfo=pytz.utc),
        "growatt_inverter_sn": ["SPH1", "SPH2"],
        "growatt_base_url": "",
        "growatt_battery_rate_max": 4800,
        "user_id": "instance-1",
        "supabase_url": "https://example.invalid",
        "supabase_key": "file-service-key",
        "unrelated": "ignored",
    }
    merged, used = merge_growatt_cli_args({"key": "cli-token", "inverter_sn": None, "base_url": None, "supabase_key": "cli-service-key"}, config)
    assert merged["key"] == "cli-token"
    assert merged["token_hash"] == "file-hash"
    # Unquoted ISO timestamps load from YAML as datetimes; OAuthMixin parses only strings and numbers
    assert merged["token_expires_at"] == "2026-11-11T20:00:00+00:00"
    assert merged["inverter_sn"] == ["SPH1", "SPH2"]
    assert merged["battery_rate_max"] == 4800
    assert merged["user_id"] == "instance-1"
    # The refresh settings come from the file too, and the command line still wins
    assert merged["supabase_url"] == "https://example.invalid"
    assert merged["supabase_key"] == "cli-service-key"
    # An empty apps.yaml value fills nothing
    assert merged["base_url"] is None
    assert "growatt_key" not in used and "growatt_base_url" not in used
    assert sorted(used) == ["growatt_battery_rate_max", "growatt_inverter_sn", "growatt_token_expires_at", "growatt_token_hash", "supabase_url", "user_id"]
    assert "unrelated" not in merged


def _cli_fake_post(calls, devices_code=0):
    """Return a GrowattAPI._post replacement answering one battery inverter, recording each endpoint."""

    async def fake_post(self, endpoint_key, body=None):
        """Answer like the Growatt cloud for the read endpoints a read-only cycle uses."""
        calls.append((endpoint_key, body, dict(self.base.args)))
        if endpoint_key == "devices":
            return (0, [{"deviceSn": "SPH123", "deviceType": "sph"}]) if devices_code == 0 else (devices_code, None)
        if endpoint_key == "device_info":
            return 0, {"model": "SPH 6000TL", "nominalPower": 6000, "existBattery": True, "batteryCapacity": 10000, "batteryNominalPower": 5000, "dischargeCutOffSOC": 10}
        if endpoint_key == "device_data":
            return 0, {
                "soc": 55,
                "batPower": 1200,
                "meterPower": 300,
                "ppv": 0,
                "payLoadPower": 900,
                "etoUserToday": 3.0,
                "etoGridToday": 0.0,
                "epvToday": 0.0,
                "batteryList": [{"echargeToday": 2.0, "edischargeToday": 1.0}],
                "utcTime": "2026-11-11 20:00:00",
            }
        if endpoint_key == "read_dispatch":
            return 0, {"value": [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]}
        return -1, None

    return fake_post


def test_growatt_cli_single_cycle():
    """One read-only run() with automatic config: binds Predbat's settings, reads the slot table back, writes nothing."""
    calls = []
    output = io.StringIO()
    with patch.object(GrowattAPI, "_post", _cli_fake_post(calls)), patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(output):
        ok = run_async_local(growatt_cli_run(key="token", token_expires_at="2099-01-01T00:00:00Z", token_hash="hash", user_id="instance-1", supabase_url="https://example.invalid", supabase_key="service", trace=False))
        assert os.environ["SUPABASE_URL"] == "https://example.invalid"
        assert os.environ["SUPABASE_KEY"] == "service"
    text = output.getvalue()
    assert ok, text
    endpoints = [call[0] for call in calls]
    assert endpoints[:3] == ["devices", "device_info", "device_data"]
    assert "read_dispatch" in endpoints
    assert "dispatch" not in endpoints, "the CLI must not write to the inverter"
    # user_id reaches the base args, where OAuthMixin's refresh reads the instance id
    assert calls[0][2]["user_id"] == "instance-1"
    assert "Set arg inverter_type = ['GrowattCloud']" in text
    assert "Set arg soc_percent = ['sensor.predbat_growatt_sph123_soc']" in text
    assert "=== SPH123 ===" in text
    assert "SoC 55.0%" in text
    assert "01:00-03:00 +50%" in text
    assert "PASSED" in text


def test_growatt_cli_failures_are_explained():
    """A run that cannot complete returns False and says which step failed."""
    # No token and no refresh route: nothing is requested at all
    calls = []
    output = io.StringIO()
    environ = {key: value for key, value in os.environ.items() if key not in ("SUPABASE_URL", "SUPABASE_KEY")}
    with patch.object(GrowattAPI, "_post", _cli_fake_post(calls)), patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(output):
        assert not run_async_local(growatt_cli_run(key=None, trace=False))
    assert calls == []
    assert "no OAuth access token" in output.getvalue()

    # The device list call is refused
    calls = []
    output = io.StringIO()
    with patch.object(GrowattAPI, "_post", _cli_fake_post(calls, devices_code=2)), contextlib.redirect_stdout(output):
        assert not run_async_local(growatt_cli_run(key="token", token_expires_at="2099-01-01T00:00:00Z", trace=False))
    assert "device list call failed" in output.getvalue()


class FakeGrowattDevice:
    """A Growatt inverter behind the OpenAPI, for the write-test driver.

    Holds enable_control and a 16-entry slot table (unused entries 00:00-00:00, as a live SPH
    reads), records every request, and reports battery power from the table: a slot's
    percentage of a 5000 W rating, Growatt's sign (positive = charging), or a 250 W discharge
    with no slot. hold_works=False makes a 0% slot behave like no slot at all. An empty slot list
    is refused (WRONG_PARAMETER_VALUE, 14) as the live device does. Telemetry carries a utcTime:
    the first stale_polls polls of a test repeat a sample taken before the write (the 250 W
    discharge), as the five-minute upload interval does; later polls are new uploads.
    """

    UNUSED = {"percentage": 0, "startTime": "00:00", "endTime": "00:00"}

    def __init__(self, enable=0, hold_works=True, fail_reads=False, raise_on_sample=None, stale_polls=0):
        """Start with the given enable_control value and an empty 16-slot table."""
        self.enable = enable
        self.table = [dict(self.UNUSED) for _ in range(16)]
        self.hold_works = hold_works
        self.fail_reads = fail_reads
        self.raise_on_sample = raise_on_sample
        self.stale_polls = stale_polls
        self.polls_since_write = 0
        self.clock = datetime.now(pytz.utc)
        self.samples = 0
        self.requests = []

    async def post(self, endpoint_key, body=None):
        """Answer one OpenAPI request the way the device would."""
        body = body or {}
        self.requests.append((endpoint_key, body.get("setType"), body.get("value")))
        if endpoint_key == "dispatch":
            if body["setType"] == GROWATT_SET_ENABLE_CONTROL:
                self.enable = body["value"]
            else:
                if body["value"] == []:
                    return 14, None
                active = [slot for slot in body["value"] if slot["startTime"] != slot["endTime"]]
                self.table = [dict(slot) for slot in active] + [dict(self.UNUSED) for _ in range(16 - len(active))]
                self.polls_since_write = 0
            return 0, None
        if endpoint_key == "read_dispatch":
            if self.fail_reads:
                return 18, None
            return 0, (self.enable if body["setType"] == GROWATT_SET_ENABLE_CONTROL else [dict(slot) for slot in self.table])
        if endpoint_key == "device_data":
            self.samples += 1
            self.polls_since_write += 1
            if self.raise_on_sample == self.samples:
                raise RuntimeError("connection dropped")
            if self.polls_since_write <= self.stale_polls:
                # The last upload, from before the write: the old 250 W discharge
                sample_time = self.clock - timedelta(minutes=1)
                bat_power = -250.0
            else:
                # A new upload each poll, 20 s apart, so every one lands inside even a short test slot
                sample_time = self.clock + timedelta(seconds=20 * self.samples)
                active = [slot for slot in self.table if slot["startTime"] != slot["endTime"]]
                percentage = active[0]["percentage"] if active else None
                if percentage is None or (percentage == 0 and not self.hold_works):
                    bat_power = -250.0
                else:
                    bat_power = percentage / 100.0 * 5000
            return 0, {"soc": 60, "batPower": bat_power, "meterPower": 0, "ppv": 0, "payLoadPower": 300, "utcTime": sample_time.strftime("%Y-%m-%d %H:%M:%S")}
        return -1, None

    def writes(self):
        """Return the (setType, value) of every dispatch write, in order."""
        return [(set_type, value) for endpoint, set_type, value in self.requests if endpoint == "dispatch"]


def _write_test(device, scenarios, tmp_dir, confirm=None, minutes=2):
    """Run growatt_write_test() against a fake device with sleeps skipped, returning (result, client)."""
    client, sn = _ready_client()
    client._post = device.post
    with patch("growatt.asyncio.sleep", new=AsyncMock()), contextlib.redirect_stdout(io.StringIO()) as output:
        result = run_async_local(growatt_write_test(client, sn, scenarios, 1000, minutes, confirm or (lambda prompt: True), tmp_dir, settle_seconds=0))
    client.cli_output = output.getvalue()
    return result, client


def test_growatt_cli_test_slots():
    """Each scenario writes one slot from this UTC minute, cut at 23:59, and none at all near midnight."""
    now = datetime(2026, 11, 11, 20, 47, 33, tzinfo=pytz.utc)
    assert build_cli_test_slots("charge", now, 3, 20) == [{"startTime": "20:47", "endTime": "20:52", "percentage": 20}]
    assert build_cli_test_slots("export", now, 3, 20) == [{"startTime": "20:47", "endTime": "20:52", "percentage": -20}]
    assert build_cli_test_slots("hold", now, 3, 20) == [{"startTime": "20:47", "endTime": "20:52", "percentage": 0}]
    late = datetime(2026, 11, 11, 23, 50, tzinfo=pytz.utc)
    assert build_cli_test_slots("charge", late, 10, 20) == [{"startTime": "23:50", "endTime": "23:59", "percentage": 20}]
    assert build_cli_test_slots("charge", datetime(2026, 11, 11, 23, 57, tzinfo=pytz.utc), 3, 20) is None


def test_growatt_cli_readings_judged_only_inside_the_slot():
    """A slot cut at 23:59 ends before the watch would: readings after its end are shown but not judged."""
    built = datetime(2026, 11, 11, 23, 52, 10, tzinfo=pytz.utc)
    slots = build_cli_test_slots("charge", built, 10, 28)
    assert slots == [{"startTime": "23:52", "endTime": "23:59", "percentage": 28}]
    slot_end = cli_slot_end(built, slots)
    assert slot_end == datetime(2026, 11, 11, 23, 59, tzinfo=pytz.utc)
    written = built + timedelta(seconds=20)
    seen = set()
    # Inside the slot: judged
    assert classify_cli_reading(-1000, datetime(2026, 11, 11, 23, 57, 0, tzinfo=pytz.utc), written, slot_end, seen)[0]
    # After the cut slot has ended (the review's t=480-600 s readings): not judged, and labelled as such
    judged, label = classify_cli_reading(20, datetime(2026, 11, 12, 0, 1, 0, tzinfo=pytz.utc), written, slot_end, seen)
    assert not judged and "after the slot ended" in label
    # Before the write, a repeat, or no reading at all: not judged
    assert not classify_cli_reading(250, datetime(2026, 11, 11, 23, 50, tzinfo=pytz.utc), written, slot_end, seen)[0]
    seen.add(datetime(2026, 11, 11, 23, 57, 0, tzinfo=pytz.utc))
    assert classify_cli_reading(-1000, datetime(2026, 11, 11, 23, 57, 0, tzinfo=pytz.utc), written, slot_end, seen) == (False, "same reading as the last poll")
    assert classify_cli_reading(None, None, written, slot_end, seen) == (False, "no reading")
    # A normal, uncut slot ends minutes + 2 after its start minute
    normal = datetime(2026, 11, 11, 21, 40, 30, tzinfo=pytz.utc)
    assert cli_slot_end(normal, build_cli_test_slots("hold", normal, 12, 28)) == datetime(2026, 11, 11, 21, 54, tzinfo=pytz.utc)


def test_growatt_cli_judge_window():
    """Settled readings decide each verdict; earlier ones are ignored."""
    assert judge_cli_window("charge", [(0, 300), (60, -900), (120, -950)])[0] == "PASS"
    assert judge_cli_window("charge", [(60, -900), (120, 20)])[0] == "FAIL"
    assert judge_cli_window("export", [(60, 950)])[0] == "PASS"
    assert judge_cli_window("export", [(60, -50)])[0] == "FAIL"
    assert judge_cli_window("hold", [(0, 250), (60, 30), (120, -40)])[0] == "PASS"
    assert judge_cli_window("hold", [(60, 250)])[0] == "FAIL"
    assert judge_cli_window("hold", [(0, 0), (30, 0)])[0] == "INCONCLUSIVE"


def test_growatt_cli_write_test_restores_each_time():
    """All three tests run, each is cleared back to the saved table, and enable_control ends as it started."""
    import tempfile

    device = FakeGrowattDevice(enable=0)
    original = [dict(slot) for slot in device.table]
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, client = _write_test(device, ["charge", "export", "hold"], tmp_dir)
        snapshots = os.listdir(tmp_dir)
        assert len(snapshots) == 1
        assert load_cli_snapshot(os.path.join(tmp_dir, snapshots[0])) == ("SPH123", 0, original)
    assert result, client.cli_output
    writes = device.writes()
    assert writes[0] == (GROWATT_SET_ENABLE_CONTROL, GROWATT_CONTROL_ON)
    percentages = [value[0]["percentage"] for set_type, value in writes if set_type == GROWATT_SET_TIME_SLOTS and value and value[0]["startTime"] != value[0]["endTime"]]
    # 1000 W of a 5000 W rating is 20%
    assert percentages == [20, -20, 0]
    # Every test is followed by a restore of the table exactly as read, placeholders included
    restores = [value for set_type, value in writes if set_type == GROWATT_SET_TIME_SLOTS and value == original]
    assert len(restores) == 3
    assert writes[-1] == (GROWATT_SET_ENABLE_CONTROL, 0)
    assert device.table == original and device.enable == 0
    assert "PASSED" in client.cli_output


def test_growatt_cli_write_test_ignores_old_readings():
    """Readings uploaded before the write are shown but never judged; the verdict waits for new uploads."""
    import tempfile

    # The old reading is a 250 W discharge, which would fail a charge test if it were judged
    device = FakeGrowattDevice(enable=1, stale_polls=3)
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, client = _write_test(device, ["charge"], tmp_dir, minutes=12)
    assert result, client.cli_output
    assert client.cli_output.count("old reading from") == 3
    assert "PASS: battery charging" in client.cli_output
    # Stops once two new readings are in: the pre-flight check, three old polls, then two new ones
    assert device.samples == 6

    # Only old readings within the 12-minute watch (13 polls): no verdict either way
    device = FakeGrowattDevice(enable=1, stale_polls=20)
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, client = _write_test(device, ["charge"], tmp_dir, minutes=12)
    assert not result
    assert "INCONCLUSIVE" in client.cli_output
    assert device.table == [dict(FakeGrowattDevice.UNUSED) for _ in range(16)]


def test_growatt_cli_write_test_refuses_when_telemetry_has_stopped():
    """A device whose last upload is over ten minutes old is not written to at all."""
    import tempfile

    # The only reading the device has is 31 minutes old, as in the live run whose datalogger stopped
    device = FakeGrowattDevice(enable=0, stale_polls=99)
    device.clock = datetime.now(pytz.utc) - timedelta(minutes=30)
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, client = _write_test(device, ["charge"], tmp_dir)
        assert os.listdir(tmp_dir) == []
    assert not result
    assert "not reporting to Growatt, so nothing was written" in client.cli_output
    assert device.writes() == []
    assert all(endpoint == "device_data" for endpoint, _, _ in device.requests)


def test_growatt_cli_write_test_failed_hold_still_restores():
    """A 0% slot that does not hold fails the hold test, and the device is still put back."""
    import tempfile

    device = FakeGrowattDevice(enable=1, hold_works=False)
    original = [dict(slot) for slot in device.table]
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, client = _write_test(device, ["hold"], tmp_dir)
    assert not result
    assert "FAIL: battery not within" in client.cli_output
    assert device.table == original and device.enable == 1
    # enable_control was already on, so it is never written
    assert all(set_type != GROWATT_SET_ENABLE_CONTROL for set_type, _ in device.writes())


def test_growatt_cli_write_test_restores_after_an_exception():
    """An exception between a write and its restore still restores the table and enable_control."""
    import tempfile

    device = FakeGrowattDevice(enable=0, raise_on_sample=2)
    original = [dict(slot) for slot in device.table]
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            _write_test(device, ["charge", "export"], tmp_dir)
            raised = False
        except RuntimeError:
            raised = True
    assert raised
    assert device.table == original and device.enable == 0


def test_growatt_cli_write_test_refuses_without_a_baseline():
    """When the current settings cannot be read there is nothing to restore, so nothing is written."""
    import tempfile

    device = FakeGrowattDevice(fail_reads=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, client = _write_test(device, ["charge"], tmp_dir)
        assert os.listdir(tmp_dir) == []
    assert not result
    assert device.writes() == []
    # Declining the prompt writes nothing either
    device = FakeGrowattDevice()
    with tempfile.TemporaryDirectory() as tmp_dir:
        result, _ = _write_test(device, ["charge"], tmp_dir, confirm=lambda prompt: False)
    assert result
    assert device.writes() == []


def test_growatt_cli_restore_snapshot():
    """A saved snapshot is written back by --restore; a file that is not one is refused."""
    import tempfile

    device = FakeGrowattDevice(enable=1)
    saved = [{"percentage": 50, "startTime": "01:00", "endTime": "03:00"}] + [dict(FakeGrowattDevice.UNUSED) for _ in range(15)]
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = save_cli_snapshot(tmp_dir, "SPH123", 0, saved)
        client, _ = _ready_client()
        client._post = device.post
        with patch("growatt.asyncio.sleep", new=AsyncMock()), contextlib.redirect_stdout(io.StringIO()):
            assert run_async_local(restore_growatt_snapshot(client, path))
        assert device.table == saved and device.enable == 0
        bad = os.path.join(tmp_dir, "bad.json")
        with open(bad, "w") as handle:
            json.dump({"something": "else"}, handle)
        try:
            load_cli_snapshot(bad)
            refused = False
        except ValueError:
            refused = True
        assert refused


def _unreadable_dispatch(client):
    """A device that takes every write but answers READ_DEVICE_PARAM_FAIL to every read, as a busy live SPH did."""
    calls = _recording_dispatch(client)

    async def read_dispatch(serial, set_type):
        """Fail every read."""
        calls.reads.append(set_type)
        return False, None

    client.read_dispatch = read_dispatch
    return calls


def test_growatt_write_backoff_doubles_and_caps():
    """Each consecutive unconfirmed table doubles the wait before the same table is tried again, up to the cap."""
    client, sn = _ready_client()
    table = [{"startTime": "01:00", "endTime": "03:00", "percentage": 50}]
    waits = [client._note_write_failure(sn, table) for _ in range(7)]
    assert waits == [1, 2, 4, 8, GROWATT_WRITE_BACKOFF_MAX, GROWATT_WRITE_BACKOFF_MAX, GROWATT_WRITE_BACKOFF_MAX]
    assert client._write_backing_off(sn, table)
    # A different table never waits
    assert not client._write_backing_off(sn, [{"startTime": "04:00", "endTime": "05:00", "percentage": 50}])
    client._clear_write_failure(sn)
    assert not client._write_backing_off(sn, table)
    assert client._write_backoff_minutes(sn) == 0


def test_growatt_unreadable_write_reads_first_then_confirms():
    """A write whose read-back failed is not re-sent blind: after the back-off the device is read first, and a table it holds is adopted."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.control_enabled.add(sn)
    client.applied_slots[sn] = []
    calls = _unreadable_dispatch(client)
    assert not run_async_local(client.apply_schedule(sn))
    assert client.last_write_outcome == GROWATT_WRITE_UNKNOWN
    target = client.build_time_slots(sn, client.local_schedule[sn])
    assert client.unverified_slots[sn] == target
    # Unknown is not taken as the device dropping control
    assert sn in client.control_enabled
    assert any("read-back failed, it may have applied" in message for message in client.log_messages)
    writes = len(calls)

    # Within the back-off the same plan sends nothing at all
    client.last_write_time[sn] = 0
    assert not run_async_local(client.apply_schedule(sn))
    assert len(calls) == writes

    # Once it expires the device answers reads again and holds the table: confirmed with a read, no write
    client.write_retry_at[sn] = 0
    client.last_write_time[sn] = 0
    reread = _recording_dispatch(client)
    reread.table = [dict(slot) for slot in target] + [dict(GROWATT_UNUSED_SLOT) for _ in range(16 - len(target))]
    assert run_async_local(client.apply_schedule(sn))
    assert reread == []
    assert reread.reads == [GROWATT_SET_TIME_SLOTS]
    assert client.applied_slots[sn] == target
    assert sn not in client.unverified_slots
    assert sn not in client.write_failures
    assert any("holds the" in message and "written earlier" in message for message in client.log_messages)


def test_growatt_unreadable_write_rewrites_when_the_device_lacks_it():
    """If the read-first shows the device does not hold the table, it is written again."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.control_enabled.add(sn)
    target = client.build_time_slots(sn, client.local_schedule[sn])
    client.unverified_slots[sn] = list(target)
    calls = _recording_dispatch(client)
    assert run_async_local(client.apply_schedule(sn))
    # One read (the table is not there), then the write and its confirming read
    assert calls.reads[0] == GROWATT_SET_TIME_SLOTS
    assert [call[1] for call in calls] == [GROWATT_SET_TIME_SLOTS]
    assert client.applied_slots[sn] == target
    assert client.last_write_outcome == GROWATT_WRITE_CONFIRMED


def test_growatt_changed_plan_skips_the_backoff():
    """A new plan is written straight away even while an older table is backing off."""
    client, sn = _ready_client()
    client.local_schedule[sn] = _schedule()
    client.control_enabled.add(sn)
    old_table = [{"startTime": "04:00", "endTime": "05:00", "percentage": 10}]
    for _ in range(3):
        client._note_write_failure(sn, old_table)
    calls = _recording_dispatch(client)
    assert run_async_local(client.apply_schedule(sn))
    assert [call[1] for call in calls] == [GROWATT_SET_TIME_SLOTS]
    assert sn not in client.write_failures
    # force also bypasses the back-off of the same table
    client.local_schedule[sn] = _schedule()
    target = client.build_time_slots(sn, client.local_schedule[sn])
    client._note_write_failure(sn, target)
    client.applied_slots.pop(sn)
    client.last_write_time[sn] = 0
    assert not run_async_local(client.apply_schedule(sn))
    client.last_write_time[sn] = 0
    assert run_async_local(client.apply_schedule(sn, force=True))


def test_growatt_rate_limit_wait_parsing():
    """The wait comes from the 105 answer's "retry after Nms", plus half a second, capped; a dispatch interval otherwise."""
    live = "Endpoint rate limited for clientId=c88737*** deviceSn=EGM4E4N08S, retry after 3551ms"
    assert abs(GrowattAPI.rate_limit_wait(live) - 4.051) < 1e-6
    assert GrowattAPI.rate_limit_wait("retry after 60000ms") == 10
    assert GrowattAPI.rate_limit_wait("") == 6
    assert GrowattAPI.rate_limit_wait(None) == 6


class _ScriptedSession(_RecordingSession):
    """A session answering each post with the next Growatt answer from a script, recording the bodies."""

    def __init__(self, urls, answers, bodies):
        """Answer from answers, recording each JSON body into bodies."""
        super().__init__(urls)
        self.answers = answers
        self.bodies = bodies

    def post(self, url, headers=None, json=None):
        """Record the URL and body, then answer with the next scripted result."""
        self.urls.append(url)
        self.bodies.append(dict(json or {}))
        answer = self.answers.pop(0)

        class _Response(_RecordingResponse):
            """A response carrying the scripted answer."""

            async def json(self):
                """Return the scripted answer."""
                return answer

        return _Response()


def test_growatt_rate_limited_request_is_retried_once_with_a_new_request_id():
    """A 105 is retried once after the wait it gives, under a new requestId; a second 105 is returned."""
    limited = {"code": 105, "message": "Endpoint rate limited for clientId=x deviceSn=SPH123, retry after 1500ms"}
    client = MockGrowatt()
    urls, bodies = [], []
    answers = [limited, {"code": 0, "data": None}]
    client.http_session = lambda **kwargs: _ScriptedSession(urls, answers, bodies)
    body = {"deviceSn": "SPH123", "setType": GROWATT_SET_TIME_SLOTS, "value": [], "requestId": GrowattAPI.request_id()}
    code, _ = run_async_local(client._request("dispatch", body))
    assert code == 0
    assert len(bodies) == 2
    assert bodies[0]["requestId"] != bodies[1]["requestId"]
    assert client.slept == [2.0]
    assert any("rate limited, retrying once" in message for message in client.log_messages)

    client = MockGrowatt()
    urls, bodies = [], []
    answers = [limited, limited]
    client.http_session = lambda **kwargs: _ScriptedSession(urls, answers, bodies)
    code, _ = run_async_local(client._request("dispatch", dict(body)))
    assert code == 105
    assert len(bodies) == 2


def test_growatt_control_event_runs_on_the_component_loop():
    """A write button pressed from another thread's loop applies the schedule on the loop that owns the write lock.

    Predbat presses the button through ha.py's run_async(), on a throwaway loop of its own. Taking the
    write lock there failed with "is bound to a different event loop" whenever run() held it.
    """
    import asyncio
    import threading

    client, sn = _ready_client()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ran_on = []
    try:
        client.component_loop = loop

        async def apply_schedule(serial, force=False):
            """Record which loop the schedule is applied on, holding the lock as the real one does."""
            async with client._write_lock:
                ran_on.append(asyncio.get_running_loop())
            return True

        client.apply_schedule = apply_schedule

        # Hold the lock on the component's loop, as run() would mid-write, so the press has to wait for it
        async def hold_lock():
            """Take the write lock on the component's loop for a moment."""
            async with client._write_lock:
                await asyncio.sleep(0.2)

        held = asyncio.run_coroutine_threadsafe(hold_lock(), loop)
        time.sleep(0.05)
        # Pressed on a throwaway loop in another thread, as ha.py does; bounded, as a press waiting
        # on another loop's lock is never woken and would otherwise hang the test
        press = threading.Thread(target=lambda: asyncio.run(client.switch_event("switch.predbat_growatt_sph123_battery_schedule_charge_write", "turn_on")), daemon=True)
        press.start()
        press.join(timeout=5)
        held.result(timeout=5)
        assert not press.is_alive(), "the press never completed"
        assert ran_on == [loop]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    # With no component loop running, the press still applies in place
    client.component_loop = None
    ran_on.clear()
    run_async_local(client.switch_event("switch.predbat_growatt_sph123_battery_schedule_charge_write", "turn_on"))
    assert len(ran_on) == 1


def run_growatt_tests(my_predbat):
    """Run the Growatt component tests in Predbat's custom test harness."""
    failed = False
    tests = [
        ("constants_utc", test_growatt_constants_and_utc_conversion),
        ("utc_midnight_split", test_growatt_utc_midnight_split),
        ("request_url_prod_api", test_growatt_request_url_includes_prod_api),
        ("stale_telemetry", test_growatt_stale_telemetry_withholds_slots),
        ("device_info_failure", test_growatt_device_info_failure_keeps_known_device),
        ("telemetry", test_growatt_telemetry_signs_and_energy_balance),
        ("slots_soc_gates", test_growatt_time_slots_use_power_percentages_and_soc_gates),
        ("charge_hold_hysteresis", test_growatt_charge_hold_hysteresis),
        ("hold_slot_written_once", test_growatt_hold_slot_is_written_once),
        ("control_round_trip", test_growatt_control_entities_round_trip),
        ("write_dedup", test_growatt_apply_schedule_enables_control_then_deduplicates),
        ("automatic_publish", test_growatt_automatic_config_and_publish),
        ("registration", test_growatt_registration_and_schema),
        ("inverter_definition", test_growatt_inverter_definition_is_complete),
        ("discovery_contract", test_growatt_discovery_matches_definition_and_automatic_config),
        ("request_id", test_growatt_request_ids_are_unique_32_character_hex),
        ("refresh_threshold", test_growatt_refresh_threshold_fits_two_hour_tokens),
        ("unknown_table_written", test_growatt_unknown_table_is_written_even_when_empty),
        ("empty_table_payload", test_growatt_empty_table_payload),
        ("write_failed_but_applied", test_growatt_write_answering_failure_but_applied_is_confirmed),
        ("write_never_applied", test_growatt_write_never_applied_is_retried_then_fails),
        ("reads_retry_busy", test_growatt_reads_retry_while_the_device_is_busy),
        ("dispatch_not_resent", test_growatt_dispatch_is_not_resent_after_a_transport_failure),
        ("state_survives_restart", test_growatt_control_state_survives_restart),
        ("restore_without_storage", test_growatt_restore_without_storage_is_a_no_op),
        ("parse_dispatch_slots", test_growatt_parse_dispatch_slots_shapes),
        ("readback_live_empty_table", test_growatt_readback_live_empty_table),
        ("readback_adopts_device", test_growatt_readback_adopts_device_table),
        ("readback_unknown_answer", test_growatt_readback_unknown_answer_keeps_written_table),
        ("readback_pacing", test_growatt_readback_waits_for_dispatch_pacing),
        ("release_control", test_growatt_release_control),
        ("run_read_only_release", test_growatt_run_releases_control_when_read_only),
        ("monitoring_only_release", test_growatt_monitoring_only_releases_claimed_control),
        ("write_backoff", test_growatt_write_backoff_doubles_and_caps),
        ("unreadable_write_reads_first", test_growatt_unreadable_write_reads_first_then_confirms),
        ("unreadable_write_rewrites", test_growatt_unreadable_write_rewrites_when_the_device_lacks_it),
        ("changed_plan_skips_backoff", test_growatt_changed_plan_skips_the_backoff),
        ("rate_limit_wait", test_growatt_rate_limit_wait_parsing),
        ("rate_limit_retry", test_growatt_rate_limited_request_is_retried_once_with_a_new_request_id),
        ("control_event_component_loop", test_growatt_control_event_runs_on_the_component_loop),
        ("zero_capacity_soc_max", test_growatt_zero_capacity_leaves_soc_max_unset),
        ("cli_merge_args", test_growatt_cli_merge_args),
        ("cli_single_cycle", test_growatt_cli_single_cycle),
        ("cli_failures", test_growatt_cli_failures_are_explained),
        ("cli_test_slots", test_growatt_cli_test_slots),
        ("cli_readings_inside_slot", test_growatt_cli_readings_judged_only_inside_the_slot),
        ("cli_judge_window", test_growatt_cli_judge_window),
        ("cli_write_test_restores", test_growatt_cli_write_test_restores_each_time),
        ("cli_write_test_old_readings", test_growatt_cli_write_test_ignores_old_readings),
        ("cli_write_test_stale_telemetry", test_growatt_cli_write_test_refuses_when_telemetry_has_stopped),
        ("cli_write_test_failed_hold", test_growatt_cli_write_test_failed_hold_still_restores),
        ("cli_write_test_exception", test_growatt_cli_write_test_restores_after_an_exception),
        ("cli_write_test_no_baseline", test_growatt_cli_write_test_refuses_without_a_baseline),
        ("cli_restore_snapshot", test_growatt_cli_restore_snapshot),
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
