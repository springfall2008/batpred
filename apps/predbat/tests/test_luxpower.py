"""Tests for the fail-closed LuxPower VPP client."""

import pytest

from luxpower import LuxpowerAPIError, LuxpowerNotReadyError, LuxpowerVppClient
from tests.test_infra import run_async


class FakeTransport:
    """Record requests and return queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __call__(self, method, url, headers, json_body, timeout_seconds):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": headers,
            "json": json_body,
            "timeout": timeout_seconds,
        })
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
    with pytest.raises(LuxpowerNotReadyError, match="binding"):
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
    with pytest.raises(LuxpowerNotReadyError, match="telemetry"):
        run_async(client.latest_telemetry("LXP-1"))
    assert transport.requests == []


def test_dispatch_fails_closed_until_partial_endpoint_is_ready():
    """A partner token alone must not activate PARTIAL control."""
    transport = FakeTransport([])
    client = LuxpowerVppClient("https://lux.example", partner_token="partner", request_impl=transport)
    with pytest.raises(LuxpowerNotReadyError, match="control"):
        run_async(client.submit_dispatch(
            "LXP-1", "CHARGE", "2026-11-07T10:00:00Z", "2026-11-07T11:00:00Z", power_limit_w=2000,
        ))
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
    result = run_async(client.submit_dispatch(
        "LXP-1",
        "DISCHARGE",
        "2026-11-07T10:00:00Z",
        "2026-11-07T11:00:00Z",
        power_limit_w=2500,
        dispatch_id=dispatch_id,
        issued_at="2026-11-07T09:59:00Z",
    ))
    assert result == {"dispatchId": dispatch_id, "location": f"/dispatches/{dispatch_id}"}
    assert transport.requests[0]["json"] == {
        "dispatchId": dispatch_id,
        "issuedAt": "2026-11-07T09:59:00Z",
        "commands": [{
            "assetId": "LXP-1",
            "mode": "DISCHARGE",
            "startAt": "2026-11-07T10:00:00Z",
            "endAt": "2026-11-07T11:00:00Z",
            "powerLimitW": 2500,
        }],
    }


@pytest.mark.parametrize("mode", ["IDLE", "SELF_CONSUMPTION"])
def test_non_power_modes_reject_a_power_limit(mode):
    """The v0.1 contract requires powerLimitW to be absent for non-power modes."""
    with pytest.raises(ValueError, match="omitted"):
        LuxpowerVppClient.build_dispatch(
            "LXP-1", mode, "2026-11-07T10:00:00Z", "2026-11-07T11:00:00Z", power_limit_w=1,
        )


def test_dispatch_rejects_non_utc_and_reversed_windows():
    """Control windows must be explicit UTC and end after start."""
    with pytest.raises(ValueError, match="UTC"):
        LuxpowerVppClient.build_dispatch(
            "LXP-1", "IDLE", "2026-11-07T10:00:00+01:00", "2026-11-07T11:00:00+01:00",
        )
    with pytest.raises(ValueError, match="after"):
        LuxpowerVppClient.build_dispatch(
            "LXP-1", "IDLE", "2026-11-07T11:00:00Z", "2026-11-07T10:00:00Z",
        )


def test_api_error_exposes_safe_problem_fields_only():
    """Problem details remain useful without including tokens or raw bodies."""
    transport = FakeTransport([(409, {
        "detail": "dispatch id conflict",
        "code": "DISPATCH_ID_CONFLICT",
        "requestId": "req-1",
    }, {})])
    client = LuxpowerVppClient("https://lux.example", partner_token="partner", control_ready=True, request_impl=transport)
    with pytest.raises(LuxpowerAPIError) as caught:
        run_async(client.get_dispatch("552bc3e7-39c6-4fae-9b49-a878919ed758"))
    assert caught.value.status == 409
    assert caught.value.code == "DISPATCH_ID_CONFLICT"
    assert caught.value.request_id == "req-1"
    assert "partner" not in str(caught.value)
