# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=line-too-long

"""Fail-closed client for the LuxPower VPP Partner API v0.1.

LuxPower currently marks binding and dispatch endpoints PARTIAL, and telemetry
PLANNED. Each family therefore has an explicit readiness gate. Merely providing
credentials never enables an unreleased operation.
"""

from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID, uuid4

import aiohttp

from component_base import ComponentBase


LUXPOWER_VPP_PATH = "/openapi/v1/vpp"
LUXPOWER_MODES = {"CHARGE", "DISCHARGE", "IDLE", "SELF_CONSUMPTION"}


class LuxpowerNotReadyError(RuntimeError):
    """Raised before network I/O when a vendor-readiness gate is closed."""


class LuxpowerAPIError(RuntimeError):
    """Safe API error which excludes credentials and raw response bodies."""

    def __init__(self, status, message, code=None, request_id=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


async def _aiohttp_transport(method, url, headers, json_body, timeout_seconds):
    """Default HTTP transport, injectable in tests."""
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, json=json_body) as response:
            try:
                payload = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                payload = {}
            return response.status, payload, dict(response.headers)


def _require_utc_timestamp(name, value):
    """Validate an RFC3339 timestamp with an explicit UTC offset."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return parsed


def _utc_now_string():
    """Return current UTC time in the API's preferred Z form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LuxpowerVppClient:
    """Mockable LuxPower discovery, binding, telemetry and dispatch client."""

    def __init__(
        self,
        base_url,
        partner_token=None,
        binding_ready=False,
        telemetry_ready=False,
        control_ready=False,
        request_impl=None,
        timeout_seconds=30,
    ):
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("LuxPower base URL is required")
        self.base_url = base_url.rstrip("/")
        self.partner_token = partner_token
        self.binding_ready = binding_ready is True
        self.telemetry_ready = telemetry_ready is True
        self.control_ready = control_ready is True
        self.request_impl = request_impl or _aiohttp_transport
        self.timeout_seconds = timeout_seconds

    async def _request(self, method, path, token, json_body=None, expected=(200,)):
        if not isinstance(token, str) or not token:
            raise ValueError("LuxPower access token is required")
        status, payload, response_headers = await self.request_impl(
            method,
            f"{self.base_url}{LUXPOWER_VPP_PATH}{path}",
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json_body,
            self.timeout_seconds,
        )
        if status not in expected:
            safe_payload = payload if isinstance(payload, dict) else {}
            code = safe_payload.get("code")
            request_id = safe_payload.get("requestId")
            detail = safe_payload.get("detail") or safe_payload.get("title") or f"LuxPower returned HTTP {status}"
            raise LuxpowerAPIError(status, str(detail), code=code, request_id=request_id)
        return payload if isinstance(payload, dict) else {}, response_headers

    def _partner_access_token(self):
        if not isinstance(self.partner_token, str) or not self.partner_token:
            raise ValueError("LuxPower partner access token is required")
        return self.partner_token

    async def list_devices(self, end_user_token):
        """List devices eligible for binding using an end-user OAuth token."""
        payload, _headers = await self._request("GET", "/devices", end_user_token)
        devices = payload.get("data", [])
        if not isinstance(devices, list):
            raise LuxpowerAPIError(200, "LuxPower device response has an invalid data field")
        return devices

    async def create_binding(self, end_user_token, asset_id, external_asset_id=None):
        """Create an asset binding only when the PARTIAL endpoint is enabled."""
        if not self.binding_ready:
            raise LuxpowerNotReadyError("LuxPower asset binding is not enabled")
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("LuxPower asset ID is required")
        body = {"assetId": asset_id}
        if external_asset_id:
            body["externalAssetId"] = external_asset_id
        payload, _headers = await self._request("POST", "/asset-bindings", end_user_token, body)
        return payload

    async def latest_telemetry(self, asset_id):
        """Fetch the latest snapshot only when the PLANNED endpoint is enabled."""
        if not self.telemetry_ready:
            raise LuxpowerNotReadyError("LuxPower telemetry is not enabled")
        path = f"/assets/{quote(asset_id, safe='')}/telemetry/latest"
        payload, _headers = await self._request("GET", path, self._partner_access_token())
        return payload

    @staticmethod
    def build_dispatch(asset_id, mode, start_at, end_at, power_limit_w=None, dispatch_id=None, issued_at=None):
        """Validate and build the v0.1 single-command dispatch body."""
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("LuxPower asset ID is required")
        mode = str(mode).upper()
        if mode not in LUXPOWER_MODES:
            raise ValueError(f"Unsupported LuxPower dispatch mode: {mode}")
        start = _require_utc_timestamp("start_at", start_at)
        end = _require_utc_timestamp("end_at", end_at)
        if end <= start:
            raise ValueError("end_at must be after start_at")
        dispatch_id = dispatch_id or str(uuid4())
        try:
            UUID(dispatch_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("dispatch_id must be a UUID") from error
        issued_at = issued_at or _utc_now_string()
        _require_utc_timestamp("issued_at", issued_at)

        command = {"assetId": asset_id, "mode": mode, "startAt": start_at, "endAt": end_at}
        if mode in {"CHARGE", "DISCHARGE"}:
            if isinstance(power_limit_w, bool) or not isinstance(power_limit_w, int) or power_limit_w <= 0:
                raise ValueError("power_limit_w must be a positive integer for charge or discharge")
            command["powerLimitW"] = power_limit_w
        elif power_limit_w is not None:
            raise ValueError("power_limit_w must be omitted for idle or self-consumption")
        return {"dispatchId": dispatch_id, "issuedAt": issued_at, "commands": [command]}

    async def submit_dispatch(self, asset_id, mode, start_at, end_at, power_limit_w=None, dispatch_id=None, issued_at=None):
        """Submit a dispatch only when the PARTIAL control API is enabled."""
        if not self.control_ready:
            raise LuxpowerNotReadyError("LuxPower dispatch control is not enabled")
        body = self.build_dispatch(asset_id, mode, start_at, end_at, power_limit_w, dispatch_id, issued_at)
        _payload, headers = await self._request("POST", "/dispatches", self._partner_access_token(), body, expected=(202,))
        return {"dispatchId": body["dispatchId"], "location": headers.get("Location") or headers.get("location")}

    async def get_dispatch(self, dispatch_id):
        """Return dispatch status behind the same PARTIAL control gate."""
        if not self.control_ready:
            raise LuxpowerNotReadyError("LuxPower dispatch control is not enabled")
        UUID(dispatch_id)
        payload, _headers = await self._request("GET", f"/dispatches/{quote(dispatch_id, safe='')}", self._partner_access_token())
        return payload

    async def cancel_dispatch(self, dispatch_id):
        """Idempotently cancel a dispatch behind the control gate."""
        if not self.control_ready:
            raise LuxpowerNotReadyError("LuxPower dispatch control is not enabled")
        UUID(dispatch_id)
        payload, _headers = await self._request("POST", f"/dispatches/{quote(dispatch_id, safe='')}/cancel", self._partner_access_token())
        return payload


class LuxpowerAPI(ComponentBase):
    """PredBat component wrapper around the fail-closed VPP client."""

    def initialize(
        self,
        asset_id,
        base_url,
        partner_token=None,
        binding_status=None,
        binding_ready=False,
        telemetry_ready=False,
        control_ready=False,
        request_impl=None,
    ):
        self.asset_id = asset_id
        self.binding_status = binding_status
        self.telemetry_ready = telemetry_ready is True
        self.control_ready = control_ready is True
        self.last_telemetry = None
        self.client = LuxpowerVppClient(
            base_url=base_url,
            partner_token=partner_token,
            binding_ready=binding_ready,
            telemetry_ready=telemetry_ready,
            control_ready=control_ready,
            request_impl=request_impl,
        )

    async def run(self, seconds, first):
        """Remain healthy but passive until LuxPower releases telemetry."""
        if not self.telemetry_ready:
            if first:
                self.log("LuxpowerAPI: telemetry and control remain disabled by readiness gates")
            self.update_success_timestamp()
            return True
        try:
            self.last_telemetry = await self.client.latest_telemetry(self.asset_id)
            self.update_success_timestamp()
            return True
        except (LuxpowerAPIError, LuxpowerNotReadyError, ValueError) as error:
            self.log(f"Warn: LuxpowerAPI: telemetry fetch failed: {error}")
            return False

    async def submit_dispatch(self, mode, start_at, end_at, power_limit_w=None, dispatch_id=None):
        """Submit a control request for this component's selected asset."""
        return await self.client.submit_dispatch(
            self.asset_id,
            mode,
            start_at,
            end_at,
            power_limit_w=power_limit_w,
            dispatch_id=dispatch_id,
        )

    async def get_dispatch(self, dispatch_id):
        """Get a dispatch for this component."""
        return await self.client.get_dispatch(dispatch_id)

    async def cancel_dispatch(self, dispatch_id):
        """Cancel a dispatch for this component."""
        return await self.client.cancel_dispatch(dispatch_id)
