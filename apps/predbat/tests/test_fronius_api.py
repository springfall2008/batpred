# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fronius Solar.web component - request layer and telemetry
# -----------------------------------------------------------------------------

"""Tests for the Fronius component's HTTP layer, error handling and telemetry parsing (``fronius.py``).

The shared test double (MockFronius) and the fake HTTP transport live here and are imported by the
other Fronius test modules.
"""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import asyncio
import json
import pytz
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from fronius import FroniusCloud
from fronius_const import FRONIUS_AUTH_BACKOFF_SECONDS, FRONIUS_ENERGY_CHANNELS, FRONIUS_RATE_LIMIT_BACKOFF_SECONDS, FRONIUS_SCOPE_CONTROL, FRONIUS_SCOPE_QUERY
from tests.test_infra import run_async

PV_ID = "a6582e07-80b1-4313-89f9-9f98fdb0a289"
SHORT_ID = "a6582e07"
KEY_ID = "FKIA-test-key-id-0000000000000000000"
KEY_VALUE = "7d1c2b3a-0000-4000-8000-123456789abc"
NOON = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

FLOW = {
    "pvSystemId": PV_ID,
    "status": {"isOnline": True, "battMode": "1"},
    "data": {
        "logDateTime": "2026-06-15T11:59:00Z",
        "channels": [
            {"channelName": "PowerFeedIn", "channelType": "Power", "unit": "W", "value": -1500.0},
            {"channelName": "PowerLoad", "channelType": "Power", "unit": "W", "value": -800.0},
            {"channelName": "PowerBattCharge", "channelType": "Power", "unit": "W", "value": -1200.0},
            {"channelName": "PowerPV", "channelType": "Power", "unit": "W", "value": 3500.0},
            {"channelName": "PowerEVCTotal", "channelType": "Power", "unit": "W", "value": None},
            {"channelName": "BattSOC", "channelType": "Percent", "unit": "%", "value": 55},
        ],
    },
}
FLOW_NULL = {"pvSystemId": PV_ID, "status": {"isOnline": True}, "data": {"logDateTime": None, "channels": [{"channelName": "BattSOC", "value": None}, {"channelName": "PowerPV", "value": None}]}}
ENERGY = {
    "pvSystemId": PV_ID,
    "data": [
        {
            "logDate": "2026-06-15",
            "channels": [
                {"channelName": "EnergyPurchased", "unit": "Wh", "value": 4000},
                {"channelName": "EnergyBattChargeGrid", "unit": "Wh", "value": 1000},
                {"channelName": "EnergyFeedIn", "unit": "Wh", "value": 6000},
                {"channelName": "EnergyBattDischargeGrid", "unit": "Wh", "value": 500},
                {"channelName": "EnergyConsumptionTotal", "unit": "Wh", "value": 9000},
                {"channelName": "EnergyProductionTotal", "unit": "Wh", "value": 15000},
            ],
        }
    ],
}
DEVICES = {
    "devices": [
        {"deviceType": "Inverter", "deviceId": "inv-1", "deviceName": "GEN24", "deviceTypeDetails": "GEN24 10.0 Plus", "serialNumber": "33460017", "nominalAcPower": 10000.0, "isActive": True},
        {"deviceType": "Battery", "deviceId": "bat-1", "deviceName": "BYD Battery-Box Premium HV", "capacity": 13824, "maxChargePower": 5632, "maxDischargePower": 5632, "maxSOC": 100, "minSOC": 5, "isActive": True},
        {"deviceType": "Battery", "deviceId": "bat-old", "capacity": 9999, "isActive": False},
    ]
}


class FakeResponse:
    """A minimal aiohttp response: status, lower-case-able headers and a text body."""

    def __init__(self, status=200, body=None, headers=None, text=None):
        """Store the canned status, headers and body (JSON-encoded unless text is given)."""
        self.status = status
        self.headers = headers or {}
        self._text = text if text is not None else ("" if body is None else json.dumps(body))

    async def text(self):
        """Return the body text."""
        return self._text

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, *args):
        """Leave the response context."""
        return False


class FakeSession:
    """A minimal aiohttp ClientSession that records each request and replays queued responses."""

    def __init__(self, transport):
        """Share the transport's response queue and call log."""
        self.transport = transport

    def request(self, method, url, headers=None, params=None, json=None):
        """Record the request and return the next queued response (or raise a queued exception)."""
        self.transport.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "params": dict(params or {}), "json": json})
        if not self.transport.responses:
            return FakeResponse(500, {"responseError": 1001, "responseMessage": "no canned response"})
        item = self.transport.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def __aenter__(self):
        """Enter the session context."""
        return self

    async def __aexit__(self, *args):
        """Leave the session context."""
        return False


class FakeTransport:
    """Stands in for aiohttp.ClientSession: every session it builds shares one queue and one call log."""

    def __init__(self, responses=None):
        """Queue the responses to replay, in order."""
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, *args, **kwargs):
        """Build a session, as aiohttp.ClientSession(timeout=...) would."""
        return FakeSession(self)

    def queue(self, *responses):
        """Append more canned responses."""
        self.responses.extend(responses)


def http(*responses):
    """Return (transport, patcher) replacing fronius's aiohttp.ClientSession with a fake."""
    transport = FakeTransport(responses)
    return transport, patch("fronius.aiohttp.ClientSession", transport)


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self):
        """Start empty."""
        self.data = {}

    async def load(self, module, name):
        """Return a stored blob or None."""
        return self.data.get((module, name))

    async def save(self, module, name, data):
        """Store a JSON round-tripped copy, as a real store would."""
        self.data[(module, name)] = json.loads(json.dumps(data))


class MockFronius(FroniusCloud):
    """Test double: a FroniusCloud without the ComponentBase lifecycle, with a settable clock."""

    def __init__(self, tz="Europe/London", now=NOON, storage=None, **kwargs):
        """Build the component against a mock base; kwargs override the initialize() args."""
        self.prefix = "predbat"
        self.log_messages = []
        self.local_tz = pytz.timezone(tz)
        self.base = MagicMock()
        self.base.args = {}
        self.api_stop = False
        self.state = {}
        self.published = {}
        self.success_updates = 0
        self._now = now
        self._storage = storage
        args = {"access_key_id": KEY_ID, "access_key_value": KEY_VALUE, "pv_system_id": PV_ID}
        args.update(kwargs)
        self.initialize(**args)

    def utc_now(self):
        """Return the test clock."""
        return self._now

    def advance(self, **delta):
        """Move the test clock forward."""
        self._now = self._now + timedelta(**delta)

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """Count success stamps instead of recording a time."""
        self.success_updates += 1

    def refresh_discovery(self):
        """No-op - build_discovery is exercised directly."""
        pass

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity instead of reaching Home Assistant."""
        self.published[entity] = {"state": state, "attributes": attributes}
        self.state[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back whatever the test (or dashboard_item) put in self.state."""
        return self.state.get(entity_id, default)

    def set_arg_auto(self, arg, value, overwrite=True):
        """Record an auto-discovered apps.yaml binding."""
        self.base.args[arg] = value

    @property
    def storage(self):
        """The fake Storage component, or None like a standalone run."""
        return self._storage


def logged(client, text):
    """Return whether any captured log line contains text."""
    return any(text in message for message in client.log_messages)


def telemetry_client(**kwargs):
    """Return a client that has read devices, power flow and energy once."""
    client = MockFronius(**kwargs)
    transport, patcher = http(FakeResponse(200, DEVICES), FakeResponse(200, FLOW), FakeResponse(200, ENERGY))
    with patcher:
        run_async(client.refresh_static())
        run_async(client.refresh_power())
        run_async(client.refresh_energy())
    assert len(transport.calls) == 3
    return client


# -----------------------------------------------------------------------------
# Request layer
# -----------------------------------------------------------------------------


def test_fronius_every_call_carries_both_key_headers():
    """Both hosts authenticate every call with AccessKeyId + AccessKeyValue, and nothing else."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(200, FLOW), FakeResponse(200, {"dispatchId": "d-1"}))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
        run_async(client._request(FRONIUS_SCOPE_CONTROL, "PUT", client._path("schedules"), body=[]))
    query, control = transport.calls
    for call in (query, control):
        assert call["headers"].get("AccessKeyId") == KEY_ID, call["headers"]
        assert call["headers"].get("AccessKeyValue") == KEY_VALUE, call["headers"]
        assert "Authorization" not in call["headers"]
    assert query["url"] == "https://swqapi.solarweb.com/pvsystems/{}/flowdata".format(PV_ID), query["url"]
    assert control["url"] == "https://swcapi.solarweb.com/pvsystems/{}/schedules".format(PV_ID), control["url"]
    assert control["method"] == "PUT"
    assert control["headers"].get("Content-Type") == "application/json"


def test_fronius_base_url_overrides():
    """fronius_query_url / fronius_control_url replace the hosts, tolerating a trailing slash."""
    client = MockFronius(query_url="https://query.example.test/", control_url="https://control.example.test")
    transport, patcher = http(FakeResponse(200, FLOW), FakeResponse(204))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
        run_async(client._request(FRONIUS_SCOPE_CONTROL, "DELETE", client._path("schedule", dispatch_id="d-1")))
    assert transport.calls[0]["url"].startswith("https://query.example.test/pvsystems/"), transport.calls[0]["url"]
    assert transport.calls[1]["url"] == "https://control.example.test/pvsystems/{}/schedules/d-1".format(PV_ID), transport.calls[1]["url"]


def test_fronius_keys_never_reach_the_log():
    """api_debug traces requests and responses, but never the credentials."""
    client = MockFronius()
    echo = {"note": "server echoed {} and {}".format(KEY_ID, KEY_VALUE)}
    transport, patcher = http(FakeResponse(200, echo), FakeResponse(401, {"responseError": 1102, "responseMessage": "AccessKey not found"}))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
        client._backoff_until.clear()
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    for message in client.log_messages:
        assert KEY_ID not in message and KEY_VALUE not in message, message
    assert logged(client, "Fronius API request")


def test_fronius_401_parks_the_scope_and_names_the_keys():
    """Bad keys are reported once with what to check, and the scope is not hammered."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(401, {"responseError": 1104, "responseMessage": "AccessKey expired."}))
    with patcher:
        first = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
        second = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    assert not first["ok"] and first["code"] == 1104, first
    assert second["skipped"], "a parked scope must not make a call"
    assert len(transport.calls) == 1, transport.calls
    assert client.auth_failed.get(FRONIUS_SCOPE_QUERY)
    assert logged(client, "fronius_access_key_id") and logged(client, "access key expired")
    assert client.api_status() == "auth_failed"
    assert "rejected" in (client.health_message() or "")
    # The park lasts FRONIUS_AUTH_BACKOFF_SECONDS, then calls resume.
    client.advance(seconds=FRONIUS_AUTH_BACKOFF_SECONDS + 1)
    transport.queue(FakeResponse(200, FLOW))
    with patcher:
        third = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    assert third["ok"] and not client.auth_failed.get(FRONIUS_SCOPE_QUERY)


def test_fronius_control_403_does_not_stop_monitoring():
    """No Flexibility API contract or consent is a control-only problem: telemetry carries on."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(403, {"responseError": 10106, "responseMessage": "User not authorized."}), FakeResponse(200, FLOW))
    with patcher:
        put = run_async(client._request(FRONIUS_SCOPE_CONTROL, "PUT", client._path("schedules"), body=[]))
        flow = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    assert not put["ok"] and flow["ok"]
    assert client.auth_failed.get(FRONIUS_SCOPE_CONTROL) and not client.auth_failed.get(FRONIUS_SCOPE_QUERY)
    assert logged(client, "Flexibility API contract") and logged(client, "monitoring continues")
    assert client.api_status() == "control_not_permitted"


def test_fronius_unknown_pv_system_points_at_the_system_id():
    """A PV system id the key cannot see is a configuration error, reported as such."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(404, {"responseError": 1015, "responseMessage": "PV system not found."}))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    assert logged(client, "fronius_pv_system_id"), client.log_messages
    assert client._in_backoff(FRONIUS_SCOPE_QUERY)


def test_fronius_429_honours_the_rate_limit_reset_header():
    """A 429 parks the scope until x-rate-limit-reset, and is logged as a pacing signal."""
    client = MockFronius()
    reset = (NOON + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S") + ".0000000Z"
    transport, patcher = http(FakeResponse(429, {"responseError": 1011, "responseMessage": "API calls quota exceeded."}, headers={"X-Rate-Limit-Reset": reset, "X-Rate-Limit-Remaining": "0"}))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    until = client._backoff_until[FRONIUS_SCOPE_QUERY]
    assert abs(until - (NOON + timedelta(minutes=10)).timestamp()) < 1, until - NOON.timestamp()
    assert client.rate_limit_remaining.get(FRONIUS_SCOPE_QUERY) == "0"
    rate_logs = [message for message in client.log_messages if "rate-limited" in message]
    assert rate_logs and rate_logs[0].startswith("Info:"), client.log_messages
    assert client.api_status() == "rate_limited"
    client.advance(minutes=9)
    assert client._in_backoff(FRONIUS_SCOPE_QUERY)
    client.advance(minutes=2)
    assert not client._in_backoff(FRONIUS_SCOPE_QUERY)


def test_fronius_429_without_header_backs_off_exponentially():
    """Without a reset header the wait starts at the default and doubles on each repeat."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(429, {}), FakeResponse(429, {}))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_CONTROL, "PUT", client._path("schedules"), body=[]))
        first = client._backoff_until[FRONIUS_SCOPE_CONTROL] - NOON.timestamp()
        client.advance(seconds=first + 1)
        run_async(client._request(FRONIUS_SCOPE_CONTROL, "PUT", client._path("schedules"), body=[]))
        second = client._backoff_until[FRONIUS_SCOPE_CONTROL] - client._now.timestamp()
    assert first == FRONIUS_RATE_LIMIT_BACKOFF_SECONDS, first
    assert second == 2 * FRONIUS_RATE_LIMIT_BACKOFF_SECONDS, second
    # The query scope is unaffected by a control rate limit.
    assert not client._in_backoff(FRONIUS_SCOPE_QUERY)


def test_fronius_detailed_error_codes_are_explained():
    """A detailed error is logged with its code, a gloss and the server's own message."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(400, {"responseError": "1008", "responseMessage": "Invalid channels: EnergyBatteryDischarge"}))
    with patcher:
        result = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("aggrdata")))
    assert result["code"] == 1008, result
    assert logged(client, "1008 (invalid channel name): Invalid channels: EnergyBatteryDischarge"), client.log_messages
    assert not client._in_backoff(FRONIUS_SCOPE_QUERY), "an ordinary request error must not park the scope"


def test_fronius_maintenance_parks_the_scope():
    """Server maintenance pauses calls rather than retrying them every tick."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(503, {"responseError": 1012, "responseMessage": "maintenance until 13:00"}))
    with patcher:
        run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    assert client._in_backoff(FRONIUS_SCOPE_QUERY) and client.api_status() == "maintenance"


def test_fronius_transport_failures_never_raise():
    """A timeout or connection error is logged and returned as a failure, never raised."""
    client = MockFronius()
    transport, patcher = http(asyncio.TimeoutError(), OSError("connection refused"))
    with patcher:
        timed_out = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
        refused = run_async(client._request(FRONIUS_SCOPE_QUERY, "GET", client._path("flowdata")))
    assert not timed_out["ok"] and not refused["ok"]
    assert logged(client, "transport failure")
    assert not client._in_backoff(FRONIUS_SCOPE_QUERY)


def test_fronius_tolerates_empty_and_non_json_bodies():
    """A 204 with no body is a success; a non-JSON error body is still handled."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(204), FakeResponse(500, text="<html>Bad gateway</html>"))
    with patcher:
        ok = run_async(client._request(FRONIUS_SCOPE_CONTROL, "DELETE", client._path("schedule", dispatch_id="d-1")))
        bad = run_async(client._request(FRONIUS_SCOPE_CONTROL, "DELETE", client._path("schedule", dispatch_id="d-1")))
    assert ok["ok"] and ok["data"] is None
    assert not bad["ok"] and bad["code"] is None


# -----------------------------------------------------------------------------
# Telemetry
# -----------------------------------------------------------------------------


def test_fronius_power_flow_signs_match_predbat():
    """Grid is negated (Fronius import-positive -> Predbat export-positive); battery passes through."""
    client = telemetry_client()
    assert client.telemetry("soc") == 55
    assert client.telemetry("grid_power") == 1500, "exporting 1500 W must read +1500"
    assert client.telemetry("battery_power") == -1200, "charging must read negative"
    assert client.telemetry("load_power") == 800
    assert client.telemetry("pv_power") == 3500
    assert client.is_online is True and client.pv_seen
    assert client.flow_time == "2026-06-15T11:59:00Z"


def test_fronius_power_flow_nulls_are_a_failed_read():
    """An all-NULL answer (Solar.web timed out on the inverter) keeps the previous values and retries."""
    client = telemetry_client()
    client.advance(minutes=6)
    transport, patcher = http(FakeResponse(200, FLOW_NULL))
    with patcher:
        ok = run_async(client.refresh_power())
    assert not ok
    assert client.telemetry("soc") == 55, "a NULL read must not overwrite the last good SoC"
    # Retried after FRONIUS_TTL_RETRY minutes, not on the very next tick.
    assert not client.tier_expired("power", 5)
    client.advance(minutes=2)
    assert client.tier_expired("power", 5)


def test_fronius_energy_request_and_parse():
    """Daily energy asks for today's local date and only the channels used, and sums the grid legs."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(200, ENERGY))
    with patcher:
        assert run_async(client.refresh_energy())
    params = transport.calls[0]["params"]
    assert params == {"from": "2026-06-15", "duration": 1, "channel": ",".join(FRONIUS_ENERGY_CHANNELS)}, params
    assert client.energy == {"import_today": 5.0, "export_today": 6.5, "load_today": 9.0, "pv_today": 15.0}, client.energy
    assert client.data_points_today == len(FRONIUS_ENERGY_CHANNELS)


def test_fronius_energy_uses_the_local_date():
    """Just after local midnight in summer the UTC date is still yesterday; the request must not be."""
    client = MockFronius(now=datetime(2026, 6, 14, 23, 30, tzinfo=timezone.utc))
    transport, patcher = http(FakeResponse(200, ENERGY))
    with patcher:
        run_async(client.refresh_energy())
    assert transport.calls[0]["params"]["from"] == "2026-06-15", transport.calls[0]["params"]


def test_fronius_static_reads_battery_and_inverter_metadata():
    """Capacity (Wh) and limits come from active batteries only; the inverter gives model and AC rating."""
    client = telemetry_client()
    assert client.battery_capacity() == 13.824, client.battery_capacity()
    assert client.battery_rate_max() == 5632
    assert client.inverter_limit() == 10000
    assert client.inverter_info.get("model") == "GEN24 10.0 Plus"
    assert client.inverter_info.get("serial") == "33460017"
    override = MockFronius(battery_rate_max=4000)
    override.battery_info = dict(client.battery_info)
    assert override.battery_rate_max() == 4000


def test_fronius_data_points_are_counted_and_reset_daily():
    """The billed data-point estimate counts each read and resets at local midnight."""
    client = telemetry_client()
    assert client.data_points_today == 1 + 1 + len(FRONIUS_ENERGY_CHANNELS), client.data_points_today
    client._now = datetime(2026, 6, 15, 23, 30, tzinfo=timezone.utc)  # 00:30 BST on the 16th
    transport, patcher = http(FakeResponse(200, FLOW))
    with patcher:
        run_async(client.refresh_power())
    assert client.data_points_today == 1, client.data_points_today


def test_fronius_failed_reads_retry_after_a_short_wait():
    """A failed tier is retried after two minutes rather than on every one-minute tick."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(500, {"responseError": 1001}))
    with patcher:
        assert not run_async(client.refresh_static())
    assert not client.tier_expired("static", 24 * 60)
    client.advance(minutes=2)
    assert client.tier_expired("static", 24 * 60)


def run_fronius_api_tests(my_predbat):
    """Run all Fronius API tests."""
    failed = False
    for name, fn in [
        ("key_headers", test_fronius_every_call_carries_both_key_headers),
        ("base_url_overrides", test_fronius_base_url_overrides),
        ("keys_not_logged", test_fronius_keys_never_reach_the_log),
        ("auth_401", test_fronius_401_parks_the_scope_and_names_the_keys),
        ("control_403", test_fronius_control_403_does_not_stop_monitoring),
        ("unknown_system", test_fronius_unknown_pv_system_points_at_the_system_id),
        ("rate_limit_header", test_fronius_429_honours_the_rate_limit_reset_header),
        ("rate_limit_exponential", test_fronius_429_without_header_backs_off_exponentially),
        ("error_codes", test_fronius_detailed_error_codes_are_explained),
        ("maintenance", test_fronius_maintenance_parks_the_scope),
        ("transport_failures", test_fronius_transport_failures_never_raise),
        ("odd_bodies", test_fronius_tolerates_empty_and_non_json_bodies),
        ("flow_signs", test_fronius_power_flow_signs_match_predbat),
        ("flow_nulls", test_fronius_power_flow_nulls_are_a_failed_read),
        ("energy_parse", test_fronius_energy_request_and_parse),
        ("energy_local_date", test_fronius_energy_uses_the_local_date),
        ("static_metadata", test_fronius_static_reads_battery_and_inverter_metadata),
        ("data_points", test_fronius_data_points_are_counted_and_reset_daily),
        ("failed_read_retry", test_fronius_failed_reads_retry_after_a_short_wait),
    ]:
        try:
            if fn():
                print(f"  FAILED: fronius_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in fronius_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
