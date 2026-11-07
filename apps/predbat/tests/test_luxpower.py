# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Tests for the fail-closed LuxPower VPP client and component wiring."""

import traceback
from unittest import TestCase

from components import Components, secret_config_names
from luxpower import LuxpowerAPI, LuxpowerAPIError, LuxpowerNotReadyError, LuxpowerVppClient
from mock_base import MockBase
from tests.test_infra import run_async


class FakeTransport:
    """Record requests and return queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __call__(self, method, url, headers, json_body, timeout_seconds):
        """Capture one outgoing request and return its queued response."""
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json_body,
                "timeout": timeout_seconds,
            }
        )
        return self.responses.pop(0)


def test_discovery_uses_end_user_token_and_documented_path():
    """Device discovery is an end-user operation, not a partner-token operation."""
    transport = FakeTransport([(200, {"data": [{"serialNum": "LXP-1"}]}, {})])
    client = LuxpowerVppClient("https://lux.example", request_impl=transport)
    devices = run_async(client.list_devices("user-token"))
    assert devices == [{"serialNum": "LXP-1"}]
    assert transport.requests[0]["url"] == "https://lux.example/openapi/v1/vpp/devices"
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer user-token"


def test_binding_fails_closed_without_network_io():
    """A documented PARTIAL endpoint stays inert until explicitly enabled."""
    transport = FakeTransport([])
    client = LuxpowerVppClient("https://lux.example", binding_ready=False, request_impl=transport)
    with TestCase().assertRaisesRegex(LuxpowerNotReadyError, "binding"):
        run_async(client.create_binding("user-token", "LXP-1", "instance-1"))
    assert transport.requests == []


def test_binding_enabled_uses_documented_request_shape():
    """Enabled binding uses serialNum as assetId and a stable external ID."""
    transport = FakeTransport([(200, {"assetId": "LXP-1", "externalAssetId": "instance-1", "state": "ACTIVE"}, {})])
    client = LuxpowerVppClient("https://lux.example", binding_ready=True, request_impl=transport)
    result = run_async(client.create_binding("user-token", "LXP-1", "instance-1"))
    assert result["state"] == "ACTIVE"
    assert transport.requests[0]["json"] == {"assetId": "LXP-1", "externalAssetId": "instance-1"}


def test_telemetry_fails_closed_until_planned_endpoint_is_ready():
    """A partner token alone must not activate the PLANNED telemetry endpoint."""
    transport = FakeTransport([])
    client = LuxpowerVppClient("https://lux.example", partner_token="partner", request_impl=transport)
    with TestCase().assertRaisesRegex(LuxpowerNotReadyError, "telemetry"):
        run_async(client.latest_telemetry("LXP-1"))
    assert transport.requests == []


def test_dispatch_fails_closed_until_partial_endpoint_is_ready():
    """A partner token alone must not activate PARTIAL control."""
    transport = FakeTransport([])
    client = LuxpowerVppClient("https://lux.example", partner_token="partner", request_impl=transport)
    with TestCase().assertRaisesRegex(LuxpowerNotReadyError, "control"):
        run_async(
            client.submit_dispatch(
                "LXP-1",
                "CHARGE",
                "2026-11-07T10:00:00Z",
                "2026-11-07T11:00:00Z",
                power_limit_w=2000,
            )
        )
    assert transport.requests == []


def test_dispatch_builds_one_valid_command_and_preserves_idempotency_key():
    """The caller's dispatch UUID is passed through unchanged for safe retries."""
    dispatch_id = "552bc3e7-39c6-4fae-9b49-a878919ed758"
    transport = FakeTransport([(202, {}, {"Location": f"/dispatches/{dispatch_id}"})])
    client = LuxpowerVppClient(
        "https://lux.example",
        partner_token="partner",
        control_ready=True,
        request_impl=transport,
    )
    result = run_async(
        client.submit_dispatch(
            "LXP-1",
            "DISCHARGE",
            "2026-11-07T10:00:00Z",
            "2026-11-07T11:00:00Z",
            power_limit_w=2500,
            dispatch_id=dispatch_id,
            issued_at="2026-11-07T09:59:00Z",
        )
    )
    assert result == {"dispatchId": dispatch_id, "location": f"/dispatches/{dispatch_id}"}
    assert transport.requests[0]["json"] == {
        "dispatchId": dispatch_id,
        "issuedAt": "2026-11-07T09:59:00Z",
        "commands": [
            {
                "assetId": "LXP-1",
                "mode": "DISCHARGE",
                "startAt": "2026-11-07T10:00:00Z",
                "endAt": "2026-11-07T11:00:00Z",
                "powerLimitW": 2500,
            }
        ],
    }


def test_non_power_modes_reject_a_power_limit():
    """The v0.1 contract requires powerLimitW to be absent for non-power modes."""
    for mode in ("IDLE", "SELF_CONSUMPTION"):
        with TestCase().assertRaisesRegex(ValueError, "omitted"):
            LuxpowerVppClient.build_dispatch(
                "LXP-1",
                mode,
                "2026-11-07T10:00:00Z",
                "2026-11-07T11:00:00Z",
                power_limit_w=1,
            )


def test_dispatch_rejects_non_utc_and_reversed_windows():
    """Control windows must be explicit UTC and end after start."""
    with TestCase().assertRaisesRegex(ValueError, "UTC"):
        LuxpowerVppClient.build_dispatch(
            "LXP-1",
            "IDLE",
            "2026-11-07T10:00:00+01:00",
            "2026-11-07T11:00:00+01:00",
        )
    with TestCase().assertRaisesRegex(ValueError, "after"):
        LuxpowerVppClient.build_dispatch(
            "LXP-1",
            "IDLE",
            "2026-11-07T11:00:00Z",
            "2026-11-07T10:00:00Z",
        )


def test_api_error_exposes_safe_problem_fields_only():
    """Problem details remain useful without including tokens or raw bodies."""
    transport = FakeTransport(
        [
            (
                409,
                {
                    "detail": "dispatch id conflict",
                    "code": "DISPATCH_ID_CONFLICT",
                    "requestId": "req-1",
                },
                {},
            )
        ]
    )
    client = LuxpowerVppClient("https://lux.example", partner_token="partner", control_ready=True, request_impl=transport)
    with TestCase().assertRaises(LuxpowerAPIError) as caught:
        run_async(client.get_dispatch("552bc3e7-39c6-4fae-9b49-a878919ed758"))
    assert caught.exception.status == 409
    assert caught.exception.code == "DISPATCH_ID_CONFLICT"
    assert caught.exception.request_id == "req-1"
    assert "partner" not in str(caught.exception)


def test_component_registry_constructs_a_passive_client():
    """The real registry constructs LuxPower without enabling network operations."""
    base = MockBase(luxpower_asset_id="LXP-1", luxpower_base_url="https://lux.example")
    registry = Components(base)
    registry.initialize(only="luxpower", phase=1)
    api = registry.get_component("luxpower")
    assert isinstance(api, LuxpowerAPI), registry.load_error("luxpower")
    transport = FakeTransport([])
    api.client.request_impl = transport
    assert api.client.binding_ready is False
    assert api.telemetry_ready is False
    assert api.control_ready is False
    assert api.last_success_timestamp is None
    assert run_async(api.run(seconds=0, first=True)) is True
    assert run_async(api.run(seconds=60, first=False)) is True
    assert api.last_success_timestamp is not None
    assert api.last_telemetry is None
    assert transport.requests == []


def test_component_fetches_enabled_telemetry():
    """The component forwards the selected asset and stores successful telemetry."""
    telemetry = {"assetId": "LXP-1", "soc": 50}
    transport = FakeTransport([(200, telemetry, {})])
    api = LuxpowerAPI(MockBase(), asset_id="LXP-1", base_url="https://lux.example", partner_token="partner", telemetry_ready=True, request_impl=transport)
    assert run_async(api.run(seconds=0, first=True)) is True
    assert api.last_telemetry == telemetry
    assert api.last_success_timestamp is not None
    assert transport.requests[0]["url"] == "https://lux.example/openapi/v1/vpp/assets/LXP-1/telemetry/latest"
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer partner"


def test_bad_configuration_is_contained_by_component_registry():
    """Malformed LuxPower configuration reports a load error without aborting startup."""
    for base_url in (" ", 8080):
        base = MockBase(luxpower_asset_id="LXP-1", luxpower_base_url=base_url)
        registry = Components(base)
        registry.initialize(only="luxpower", phase=1)
        assert registry.get_component("luxpower") is None
        assert registry.load_error("luxpower") == "LuxPower base URL is required"
        registry.initialize(only="storage", phase=0)
        assert registry.get_component("storage") is not None


def test_partner_token_is_registered_as_a_secret():
    """Debug exports must redact the credential used for partner operations."""
    assert "luxpower_partner_token" in secret_config_names()


def run_luxpower_tests(my_predbat=None):
    """Run every LuxPower test through the repository runner, returning True on failure."""
    tests = sorted((name, test) for name, test in globals().items() if name.startswith("test_") and callable(test))
    failed = False
    for name, test in tests:
        try:
            test()
            print(f"  OK: {name}")
        except Exception:
            failed = True
            print(f"  FAIL: {name}")
            traceback.print_exc()
    return failed
