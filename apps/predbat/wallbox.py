# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Wallbox EV charger cloud API library.
# The API is the one the Home Assistant wallbox integration and the wallbox PyPI
# library use; it is reimplemented here over aiohttp.
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Wallbox EV charger integration.

Monitors every charger on a Wallbox account, wires them into Predbat's car charging
inputs, exposes pause/resume, lock, charging current and Eco-Smart controls, and can
pause and resume each charger from Predbat's own car charging plan.
"""

import argparse
import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from predbat_metrics import record_api_call

WALLBOX_AUTH_URL = "https://user-api.wall-box.com/"
WALLBOX_API_URL = "https://api.wall-box.com/"

API_TIMEOUT = 30
USER_AGENT = "Predbat"

# A token is treated as expired this long before Wallbox says it is, so a call never
# leaves with a token that dies in flight
TOKEN_MARGIN_SECONDS = 120


# Status id to (text, car connected). The text is the Home Assistant wallbox integration's
# wording, so states match what existing users already see. A None status id means the
# charger reported nothing, which Wallbox treats as disconnected.
WALLBOX_STATUS = {
    0: ("Disconnected", False),
    14: ("Error", False),
    15: ("Error", False),
    161: ("Ready", False),
    162: ("Ready", False),
    163: ("Disconnected", False),
    164: ("Waiting", True),
    165: ("Locked", False),
    166: ("Updating", False),
    177: ("Scheduled", True),
    178: ("Paused", True),
    179: ("Scheduled", True),
    180: ("Waiting for car demand", True),
    181: ("Waiting for car demand", True),
    182: ("Paused", True),
    183: ("Waiting in queue by Power Sharing", True),
    184: ("Waiting in queue by Power Sharing", True),
    185: ("Waiting in queue by Power Boost", True),
    186: ("Waiting in queue by Power Boost", True),
    187: ("Waiting MID failed", True),
    188: ("Waiting MID safety margin exceeded", True),
    189: ("Waiting in queue by Eco-Smart", True),
    193: ("Charging", True),
    194: ("Charging", True),
    195: ("Charging", True),
    196: ("Discharging", True),
    209: ("Locked", False),
    210: ("Locked, car connected", True),
}
CHARGING_STATUS_IDS = (193, 194, 195)
PAUSED_STATUS_IDS = (178, 182)

ECO_SMART_OFF = "off"
ECO_SMART_ECO = "eco_mode"
ECO_SMART_FULL_SOLAR = "full_solar"
ECO_SMART_OPTIONS = [ECO_SMART_OFF, ECO_SMART_ECO, ECO_SMART_FULL_SOLAR]


def _to_float(value, default=0.0):
    """Convert a value to float, returning the default when it is missing or not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default=0):
    """Convert a value to int, returning the default when it is missing or not a number."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class WallboxCharger:
    """One charger's state, normalised from a Wallbox status payload."""

    charger_id: str
    name: str
    serial: str
    part_number: str
    software_version: str
    status_id: int
    status: str
    connected: bool
    charging: bool
    paused: bool
    locked: bool
    power_w: float
    session_energy_kwh: float
    max_charging_current: int
    max_available_current: int
    eco_smart: Optional[str]


def normalise_charger(charger_id, payload):
    """Turn one status payload into a WallboxCharger, with safe defaults for anything missing."""
    if not isinstance(payload, dict):
        payload = {}
    config = payload.get("config_data")
    if not isinstance(config, dict):
        config = {}
    software = config.get("software")
    if not isinstance(software, dict):
        software = {}

    status_id = payload.get("status_id")
    if status_id is None:
        status_id = 0
    status_id = _to_int(status_id, -1)
    status, connected = WALLBOX_STATUS.get(status_id, ("Unknown", False))

    eco_smart = None
    eco_block = config.get("ecosmart")
    if isinstance(eco_block, dict) and eco_block.get("mode") is not None:
        if not eco_block.get("enabled"):
            eco_smart = ECO_SMART_OFF
        elif _to_int(eco_block.get("mode"), 0) == 1:
            eco_smart = ECO_SMART_FULL_SOLAR
        else:
            eco_smart = ECO_SMART_ECO

    return WallboxCharger(
        charger_id=str(charger_id),
        name=str(payload.get("name") or "Wallbox {}".format(charger_id)),
        serial=str(config.get("serial_number") or ""),
        part_number=str(config.get("part_number") or ""),
        software_version=str(software.get("currentVersion") or ""),
        status_id=status_id,
        status=status,
        connected=connected,
        charging=status_id in CHARGING_STATUS_IDS,
        paused=status_id in PAUSED_STATUS_IDS,
        locked=bool(config.get("locked")),
        power_w=round(_to_float(payload.get("charging_power")) * 1000.0, 1),
        session_energy_kwh=_to_float(payload.get("added_energy")),
        max_charging_current=_to_int(config.get("max_charging_current")),
        max_available_current=_to_int(payload.get("max_available_power")),
        eco_smart=eco_smart,
    )


class WallboxError(Exception):
    """Base class for every Wallbox API failure."""


class WallboxAuthError(WallboxError):
    """Wallbox rejected the credentials or the token."""


class WallboxRateLimitError(WallboxError):
    """Wallbox answered 429, too many requests."""


class WallboxPermissionError(WallboxError):
    """Wallbox refused a write because the account lacks admin rights over the charger."""


class WallboxApiError(WallboxError):
    """Any other failure: a bad status, a timeout, a dropped connection or an unreadable body."""


def basic_auth_header(username, password):
    """Build an HTTP Basic Authorization header value, encoded as UTF-8."""
    token = base64.b64encode("{}:{}".format(username, password).encode("utf-8")).decode("ascii")
    return "Basic " + token


class WallboxTransport:
    """Owns the Wallbox tokens and performs every HTTP call."""

    def __init__(self, log, username, password):
        """Store the credentials and start with no token."""
        self.log = log
        self.username = username
        self.password = password
        self.token = None
        self.refresh_token = None
        # Absolute expiry times, in epoch seconds
        self.token_expiry = 0
        self.refresh_expiry = 0

    def _clear_tokens(self):
        """Forget both tokens so the next call signs in from scratch."""
        self.token = None
        self.refresh_token = None
        self.token_expiry = 0
        self.refresh_expiry = 0

    def _store_tokens(self, payload):
        """Take the tokens and their expiry times from a sign in or refresh response."""
        try:
            attributes = payload["data"]["attributes"]
            self.token = attributes["token"]
            self.refresh_token = attributes.get("refresh_token")
            # Wallbox sends absolute expiry times in epoch milliseconds
            self.token_expiry = float(attributes["ttl"]) / 1000.0
            self.refresh_expiry = float(attributes.get("refresh_token_ttl", 0)) / 1000.0
        except (KeyError, TypeError, ValueError) as exc:
            self._clear_tokens()
            raise WallboxApiError("unexpected sign in response from Wallbox") from exc

    async def _call(self, method, url, headers, body=None, write=False):
        """Perform one HTTP request and return its decoded JSON object.

        Args:
            method: The HTTP method.
            url: The full URL.
            headers: The request headers.
            body: A JSON-serialisable body, or None.
            write: True for a control call. A 403 is then a rights problem rather than
                bad credentials, and an empty response body is accepted.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    status = response.status
                    if status == 429:
                        record_api_call("wallbox", success=False, reason="rate_limit")
                        raise WallboxRateLimitError("Wallbox rate limit reached calling {}".format(url))
                    if status == 403 and write:
                        record_api_call("wallbox", success=False, reason="auth_error")
                        raise WallboxPermissionError("Wallbox refused {} - the account needs admin rights over the charger".format(url))
                    if status in (401, 403):
                        record_api_call("wallbox", success=False, reason="auth_error")
                        raise WallboxAuthError("Wallbox rejected the credentials for {}".format(url))
                    if status < 200 or status >= 300:
                        record_api_call("wallbox", success=False, reason="server_error" if status >= 500 else "client_error")
                        raise WallboxApiError("HTTP {} from {}".format(status, url))
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        if write:
                            payload = {}
                        else:
                            record_api_call("wallbox", success=False, reason="decode_error")
                            raise WallboxApiError("unreadable response from {}".format(url)) from exc
                    if not isinstance(payload, dict):
                        if write:
                            payload = {}
                        else:
                            record_api_call("wallbox", success=False, reason="decode_error")
                            raise WallboxApiError("unexpected response from {}".format(url))
                    record_api_call("wallbox", success=True)
                    return payload
        except asyncio.TimeoutError as exc:
            record_api_call("wallbox", success=False, reason="connection_error")
            raise WallboxApiError("timed out calling {}".format(url)) from exc
        except aiohttp.ClientError as exc:
            record_api_call("wallbox", success=False, reason="connection_error")
            raise WallboxApiError("request to {} failed: {}".format(url, exc)) from exc

    async def authenticate(self):
        """Make sure there is a usable token: reuse it, refresh it, or sign in."""
        now = time.time()
        if self.token and self.token_expiry - TOKEN_MARGIN_SECONDS > now:
            return
        if self.refresh_token and self.refresh_expiry - TOKEN_MARGIN_SECONDS > now:
            headers = {"Authorization": "Bearer {}".format(self.refresh_token), "Partner": "wallbox", "Accept": "application/json", "User-Agent": USER_AGENT}
            try:
                self._store_tokens(await self._call("GET", WALLBOX_AUTH_URL + "users/refresh-token", headers))
                return
            except WallboxAuthError:
                self.log("Info: wallbox: the refresh token was rejected, signing in again")
        self._clear_tokens()
        headers = {"Authorization": basic_auth_header(self.username, self.password), "Partner": "wallbox", "Accept": "application/json", "User-Agent": USER_AGENT}
        self._store_tokens(await self._call("GET", WALLBOX_AUTH_URL + "users/signin", headers))

    def _headers(self):
        """The headers every data call carries."""
        return {
            "Authorization": "Bearer {}".format(self.token),
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": USER_AGENT,
        }

    async def _api(self, method, path, body=None, write=False):
        """Perform one authenticated data call, signing in again once if the token was revoked."""
        await self.authenticate()
        try:
            return await self._call(method, WALLBOX_API_URL + path, self._headers(), body=body, write=write)
        except WallboxAuthError:
            # The token was revoked before its stated expiry. Start again once.
            self._clear_tokens()
            await self.authenticate()
            return await self._call(method, WALLBOX_API_URL + path, self._headers(), body=body, write=write)

    async def list_chargers(self):
        """Return the id of every charger on the account, across all groups."""
        payload = await self._api("GET", "v3/chargers/groups")
        charger_ids = []
        result = payload.get("result")
        groups = result.get("groups") if isinstance(result, dict) else None
        for group in groups or []:
            for charger in (group or {}).get("chargers") or []:
                if isinstance(charger, dict) and charger.get("id") is not None:
                    charger_ids.append(charger["id"])
        return charger_ids

    async def get_status(self, charger_id):
        """Return the raw status payload of one charger."""
        return await self._api("GET", "chargers/status/{}".format(charger_id))


async def run_wallbox_cli(args):  # pragma: no cover
    """Sign in, list the chargers and print each one's status against the live API."""
    transport = WallboxTransport(print, args.username, args.password)
    charger_ids = await transport.list_chargers()
    if not charger_ids:
        print("No chargers found on this account")
        return
    print("Chargers: {}".format(charger_ids))
    for charger_id in charger_ids:
        payload = await transport.get_status(charger_id)
        if args.raw:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print("{}: status_id={} power={}kW added_energy={}kWh".format(charger_id, payload.get("status_id"), payload.get("charging_power"), payload.get("added_energy")))


def main():  # pragma: no cover
    """Main function for command line execution."""
    parser = argparse.ArgumentParser(description="Test the Wallbox API")
    parser.add_argument("--username", required=True, help="Wallbox account email address")
    parser.add_argument("--password", required=True, help="Wallbox account password")
    parser.add_argument("--raw", action="store_true", help="Print each charger's full status payload as JSON")
    args = parser.parse_args()
    asyncio.run(run_wallbox_cli(args))


if __name__ == "__main__":
    main()
