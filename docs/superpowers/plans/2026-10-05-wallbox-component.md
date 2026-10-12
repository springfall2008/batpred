# Wallbox Component Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `wallbox` cloud component to Predbat that monitors every Wallbox charger on an account, wires them into car charging, exposes manual controls, and can pause/resume each charger from Predbat's car plan.

**Architecture:** One new file, `apps/predbat/wallbox.py`, in the shape of `myenergi.py`: a `WallboxTransport` that owns the tokens and every `aiohttp` call, a pure `normalise_charger()` that turns a status payload into a `WallboxCharger` dataclass, and a `WallboxAPI(ComponentBase)` that polls, publishes, handles control events, auto-configures and runs plan-led control. The component never touches `aiohttp`; the transport never touches Predbat.

**Tech Stack:** Python 3, `aiohttp` (already a dependency, `>=3.12`), Predbat's `ComponentBase`, `MockBase`, `predbat_metrics.record_api_call`, `utils.parse_car_plan_windows`, the Storage component.

**Spec:** `docs/superpowers/specs/2026-10-05-wallbox-component-design.md` — read it before starting. This plan argues from it.

## Global Constraints

- No new dependency. Do not import `wallbox`, `requests` or `aenum`.
- Auth host `https://user-api.wall-box.com/`, API host `https://api.wall-box.com/`. Sign in and refresh send the header `Partner: wallbox`.
- Config keys are exactly: `wallbox_username`, `wallbox_password`, `wallbox_automatic` (default `True`), `wallbox_control` (default `False`), `wallbox_poll_seconds` (default `120`, rounded to a multiple of 60, clamped to 60–1800). There is no `wallbox_enable_controls`.
- Entity names are `{prefix}_wallbox_{charger_id}_…`; the global switch is `switch.{prefix}_wallbox_control`. Every entity is published with `dashboard_item(..., app="wallbox")`.
- No state-of-charge sensor and no `car_charging_soc` wiring: a Type 2 connector cannot report it.
- Resume is sent only to a Paused charger (status 178, 182). Never to Scheduled or Waiting. Predbat never unlocks a charger.
- Component log lines start `Info: wallbox:`, `Warn: wallbox:` or `Error: wallbox:`.
- Every function and class needs a docstring (`interrogate` enforces 100%). Line length up to 250. British English in docs and comments.
- Variable names `lower_case_with_underscores`.
- Before editing an existing symbol (`COMPONENT_LIST`, `APPS_SCHEMA`, `TEST_REGISTRY`) run GitNexus `impact({target: "<symbol>", direction: "upstream"})` and report the blast radius; before each commit run GitNexus `detect_changes()`. This is a repository rule in `CLAUDE.md`.
- Tests are slow. Always write test output to a file and read the file; never pipe a test run straight into `grep`.
- Work on a branch, not `main`: `git checkout -b feat/wallbox-component` before Task 1.

## Review Focus

Inputs the spec implies but does not spell out. Each has a test in the task named.

1. A status payload with `null` or missing numbers (`charging_power: null`, no `config_data`) — the charger is still published with zeros, never an exception. → Task 3.
2. One charger's status call fails while another's succeeds — the healthy charger is still published, charger N is still car N, and control skips the stale one. → Task 5.
3. A charger with no matching car slot sensor (more chargers than planned cars, or Predbat has not planned yet) — it is left alone, not treated as "outside a window" and paused. → Task 8.
4. A password containing `:` or non-ASCII characters — the Basic header is still built correctly as UTF-8. → Task 1.
5. A number entity set to a non-numeric or out-of-range value — ignored or clamped, no API call with a bad value. → Task 6.

## File Structure

| File | Responsibility |
|---|---|
| `apps/predbat/wallbox.py` (create) | Transport, normaliser, component, CLI |
| `apps/predbat/tests/test_wallbox.py` (create) | All unit tests, one `test_wallbox()` entry point |
| `apps/predbat/unit_test.py` (modify) | Import and `TEST_REGISTRY` line |
| `apps/predbat/components.py` (modify) | `COMPONENT_LIST["wallbox"]` |
| `apps/predbat/config.py` (modify) | Five `APPS_SCHEMA` keys |
| `apps/predbat/config/apps.yaml` (modify) | Commented example block |
| `docs/components.md`, `docs/car-charging.md`, `docs/apps-yaml.md`, `docs/devices.md` (modify) | User documentation |
| `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` (modify) | Component count and name lists |

How to run the tests (used by every task):

```bash
cd coverage
./run_all --test wallbox > /tmp/wallbox_test.log 2>&1; tail -30 /tmp/wallbox_test.log
```

A pass ends with the `test_wallbox` summary and no traceback. A failure shows a traceback in the log file.

---

### Task 1: Transport — authentication and reads

**Files:**
- Create: `apps/predbat/wallbox.py`
- Create: `apps/predbat/tests/test_wallbox.py`
- Modify: `apps/predbat/unit_test.py` (import near line 278, registry near line 709, next to the `myenergi` lines)

**Interfaces:**
- Produces: `WallboxError`, `WallboxAuthError`, `WallboxRateLimitError`, `WallboxPermissionError`, `WallboxApiError`; `basic_auth_header(username, password) -> str`; `WallboxTransport(log, username, password)` with `async authenticate()`, `async list_chargers() -> list`, `async get_status(charger_id) -> dict`, and the internal `async _api(method, path, body=None, write=False) -> dict` that Task 4 builds on.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_wallbox.py`:

```python
# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the Wallbox EV charger integration
"""

import asyncio
import datetime
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytz

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async
from mock_base import MockBase

from wallbox import (
    WALLBOX_API_URL,
    WALLBOX_AUTH_URL,
    WallboxApiError,
    WallboxAuthError,
    WallboxRateLimitError,
    WallboxTransport,
    basic_auth_header,
)


def _response(json_data=None, status=200, json_error=None):
    """Build a mock aiohttp response usable as an async context manager."""
    response = MagicMock()
    response.status = status
    if json_error is not None:
        response.json = AsyncMock(side_effect=json_error)
    else:
        response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _session(responses):
    """Build a mock aiohttp session whose request() returns the next queued response.

    A queued exception instance is raised instead. Returns (session, calls), where calls
    records the method, URL, headers and JSON body of every request in order.
    """
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        """Record the request, then return or raise the next queued item."""
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}, "json": kwargs.get("json")})
        item = queue.pop(0) if queue else _response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def _auth_payload(token="jwt-1", refresh="refresh-1", life=3600, refresh_life=86400):
    """Build a sign in response whose TTLs are absolute epoch milliseconds, as Wallbox sends them."""
    now = time.time()
    return {"data": {"attributes": {"token": token, "refresh_token": refresh, "ttl": int((now + life) * 1000), "refresh_token_ttl": int((now + refresh_life) * 1000)}}}


MOCK_GROUPS = {"result": {"groups": [{"chargers": [{"id": 202}, {"id": 101}]}, {"chargers": []}]}}


def test_basic_auth_header():
    """The Basic header is UTF-8, so a colon or accented character in the password survives."""
    assert basic_auth_header("user@example.com", "secret") == "Basic dXNlckBleGFtcGxlLmNvbTpzZWNyZXQ="
    # base64 of "a:pä:ss" encoded as UTF-8
    assert basic_auth_header("a", "pä:ss") == "Basic YTpww6Q6c3M="
    print("  ✓ Basic auth header is built as UTF-8")


def test_transport_signs_in_then_lists_chargers():
    """The first call signs in with Basic auth and the Partner header, then uses the bearer token."""
    session, calls = _session([_response(_auth_payload()), _response(MOCK_GROUPS)])
    transport = WallboxTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        chargers = run_async(transport.list_chargers())

    assert chargers == [202, 101], chargers
    assert calls[0]["url"] == WALLBOX_AUTH_URL + "users/signin"
    assert calls[0]["headers"]["Authorization"] == basic_auth_header("user@example.com", "secret")
    assert calls[0]["headers"]["Partner"] == "wallbox"
    assert calls[1]["url"] == WALLBOX_API_URL + "v3/chargers/groups"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-1"
    print("  ✓ Transport signs in then lists chargers")


def test_transport_reuses_a_valid_token():
    """A token with life left is reused: two reads make one sign in."""
    session, calls = _session([_response(_auth_payload()), _response({"status_id": 193}), _response({"status_id": 161})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        first = run_async(transport.get_status(101))
        second = run_async(transport.get_status(101))

    assert first == {"status_id": 193} and second == {"status_id": 161}
    assert [call["url"] for call in calls] == [WALLBOX_AUTH_URL + "users/signin", WALLBOX_API_URL + "chargers/status/101", WALLBOX_API_URL + "chargers/status/101"]
    print("  ✓ A valid token is reused")


def test_transport_refreshes_an_expired_token():
    """An expired token with a live refresh token is refreshed with a bearer call, not a sign in."""
    session, calls = _session([_response(_auth_payload(token="jwt-2")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() - 10
    transport.refresh_token = "refresh-1"
    transport.refresh_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        run_async(transport.get_status(101))

    assert calls[0]["url"] == WALLBOX_AUTH_URL + "users/refresh-token"
    assert calls[0]["headers"]["Authorization"] == "Bearer refresh-1"
    assert calls[0]["headers"]["Partner"] == "wallbox"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-2"
    print("  ✓ An expired token is refreshed")


def test_transport_signs_in_when_the_refresh_is_rejected():
    """A 401 on refresh means the refresh token was revoked: fall back to a full sign in."""
    session, calls = _session([_response({}, status=401), _response(_auth_payload(token="jwt-3")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() - 10
    transport.refresh_token = "refresh-1"
    transport.refresh_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        run_async(transport.get_status(101))

    assert [call["url"] for call in calls] == [WALLBOX_AUTH_URL + "users/refresh-token", WALLBOX_AUTH_URL + "users/signin", WALLBOX_API_URL + "chargers/status/101"]
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-3"
    print("  ✓ A rejected refresh falls back to sign in")


def test_transport_bad_credentials():
    """A 403 on sign in is an auth error, recorded with reason auth_error."""
    session, _ = _session([_response({}, status=403)])
    transport = WallboxTransport(print, "user@example.com", "wrong")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call") as mock_record:
        try:
            run_async(transport.list_chargers())
            raise AssertionError("Expected WallboxAuthError")
        except WallboxAuthError:
            pass
    assert mock_record.call_args.kwargs.get("reason") == "auth_error"
    print("  ✓ Bad credentials raise WallboxAuthError")


def test_transport_retries_once_after_a_revoked_token():
    """A 401 on a data call drops the token, signs in again and retries the call once."""
    session, calls = _session([_response({}, status=401), _response(_auth_payload(token="jwt-9")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        status = run_async(transport.get_status(101))

    assert status == {"status_id": 193}
    assert calls[1]["url"] == WALLBOX_AUTH_URL + "users/signin"
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-9"
    print("  ✓ A revoked token is replaced and the call retried once")


def test_transport_rate_limit_and_failures():
    """429 is its own error; a 500, a timeout, a dropped connection and a non-JSON body are API errors."""
    cases = [
        (_response({}, status=429), WallboxRateLimitError, "rate_limit"),
        (_response({}, status=500), WallboxApiError, "server_error"),
        (_response({}, status=404), WallboxApiError, "client_error"),
        (asyncio.TimeoutError(), WallboxApiError, "connection_error"),
        (aiohttp.ClientError("boom"), WallboxApiError, "connection_error"),
        (_response(json_error=ValueError("not json")), WallboxApiError, "decode_error"),
        (_response(["not", "a", "dict"]), WallboxApiError, "decode_error"),
    ]
    for item, expected, reason in cases:
        session, _ = _session([item])
        transport = WallboxTransport(print, "user@example.com", "secret")
        transport.token = "jwt-1"
        transport.token_expiry = time.time() + 3600
        with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call") as mock_record:
            try:
                run_async(transport.get_status(101))
                raise AssertionError("Expected {}".format(expected.__name__))
            except expected:
                pass
        assert mock_record.call_args.kwargs.get("reason") == reason, (expected, mock_record.call_args)
    print("  ✓ Rate limit and failures map to the right errors and metric reasons")


def test_wallbox(my_predbat=None):
    """Run every Wallbox test."""
    print("=" * 70)
    print("Wallbox tests")
    print("=" * 70)
    test_basic_auth_header()
    test_transport_signs_in_then_lists_chargers()
    test_transport_reuses_a_valid_token()
    test_transport_refreshes_an_expired_token()
    test_transport_signs_in_when_the_refresh_is_rejected()
    test_transport_bad_credentials()
    test_transport_retries_once_after_a_revoked_token()
    test_transport_rate_limit_and_failures()
    print("=" * 70)
    return False
```

In `apps/predbat/unit_test.py`, add next to the `myenergi` import and registry lines:

```python
from tests.test_wallbox import test_wallbox
```

```python
    ("wallbox", test_wallbox, "Wallbox EV charger tests (transport, normalisation, publishing, auto-config, controls, plan-led charging)", False),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run the test command from "How to run the tests".
Expected: a traceback ending `ModuleNotFoundError: No module named 'wallbox'`.

- [ ] **Step 3: Write the implementation**

Create `apps/predbat/wallbox.py`:

```python
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

import asyncio
import base64
import time

import aiohttp

from predbat_metrics import record_api_call

WALLBOX_AUTH_URL = "https://user-api.wall-box.com/"
WALLBOX_API_URL = "https://api.wall-box.com/"

API_TIMEOUT = 30
USER_AGENT = "Predbat"

# A token is treated as expired this long before Wallbox says it is, so a call never
# leaves with a token that dies in flight
TOKEN_MARGIN_SECONDS = 120


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the test command. Expected: eight `✓` lines and no traceback.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py apps/predbat/unit_test.py
git commit -m "feat(wallbox): transport with sign in, token refresh and charger reads"
```

---

### Task 2: Command line mode and payload capture (needs the maintainer's charger)

**Files:**
- Modify: `apps/predbat/wallbox.py` (append the CLI)
- Modify: `apps/predbat/tests/test_wallbox.py` (add the captured fixtures)

**Interfaces:**
- Consumes: `WallboxTransport.list_chargers()`, `.get_status()`.
- Produces: `python3 wallbox.py --username … --password … [--raw]`; test constants `MOCK_STATUS_CHARGING`, `MOCK_STATUS_READY`, `MOCK_STATUS_PAUSED` used by later tasks through the `_status()` helper defined in Task 3.

This task has a human step. The command line code is `# pragma: no cover`, as in `myenergi.py`, so it has no unit test; its test is the live run.

- [ ] **Step 1: Add the command line mode**

Add `import argparse` and `import json` to the imports of `wallbox.py`, and append:

```python
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
```

- [ ] **Step 2: Capture real payloads (maintainer)**

From `apps/predbat/`, run this three times: with the car charging, with the car unplugged, and with the car plugged in and charging paused from the Wallbox app.

```bash
python3 wallbox.py --username <email> --password <password> --raw > /tmp/wallbox_charging.json
```

Expected: `Chargers: [<id>]` followed by a JSON object containing at least `status_id`, `charging_power`, `added_energy`, `max_available_power`, `name` and a `config_data` object with `max_charging_current`, `locked`, `serial_number`, `part_number` and `software`.

Also confirm the sign in worked with `User-Agent: Predbat`. If Wallbox rejects it (the sign in fails but the same credentials work in the Wallbox app), change `USER_AGENT` to `"HomeAssistantWallboxPlugin/1.0.0"` and record that in `tools/debug-journal.md`.

- [ ] **Step 3: Add the captures as fixtures**

Add the three payloads to `test_wallbox.py` as `MOCK_STATUS_CHARGING`, `MOCK_STATUS_READY` and `MOCK_STATUS_PAUSED`, below `MOCK_GROUPS`. Before pasting, replace the charger id with `101`, `serial_number` with `"900001"`, `name` with `"Garage"`, and delete any field holding an address, a location, an email, a user id or a token. Keep every other field exactly as captured, including ones this plan never reads.

- [ ] **Step 4: Check the capture against the spec**

Compare the captured field names with spec section 2.3. If a field the spec reads is missing or named differently (for example `max_charging_current` is not under `config_data`), stop and report it: the normaliser in Task 3 and the spec both need correcting before going on. Record the paused capture's `status_id`; it must be 178 or 182, and if it is anything else, stop and report it, because spec section 2.4 is then wrong.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): command line mode and captured status fixtures"
```

---

### Task 3: Normaliser and status table

**Files:**
- Modify: `apps/predbat/wallbox.py`
- Modify: `apps/predbat/tests/test_wallbox.py`

**Interfaces:**
- Produces: `WALLBOX_STATUS` (dict of status id to `(text, connected)`), `CHARGING_STATUS_IDS = (193, 194, 195)`, `PAUSED_STATUS_IDS = (178, 182)`, `ECO_SMART_OPTIONS = ["off", "eco_mode", "full_solar"]`, and

```python
@dataclass
class WallboxCharger:
    charger_id: str           # str(id), used in entity names
    name: str
    serial: str
    part_number: str
    software_version: str
    status_id: int
    status: str               # the Home Assistant wording
    connected: bool
    charging: bool
    paused: bool
    locked: bool
    power_w: float
    session_energy_kwh: float
    max_charging_current: int
    max_available_current: int
    eco_smart: Optional[str]  # one of ECO_SMART_OPTIONS, or None when unsupported

def normalise_charger(charger_id, payload) -> WallboxCharger
```

- Test helper produced for later tasks: `_status(status_id=193, power=7.2, energy=4.5, locked=False, max_current=32, eco=None, name="Garage") -> dict`.

- [ ] **Step 1: Write the failing tests**

Add to the `from wallbox import (...)` list: `CHARGING_STATUS_IDS`, `PAUSED_STATUS_IDS`, `WALLBOX_STATUS`, `WallboxCharger`, `normalise_charger`. Add:

```python
def _status(status_id=193, power=7.2, energy=4.5, locked=False, max_current=32, eco=None, name="Garage"):
    """Build a status payload in the shape Wallbox returns, for the fields Predbat reads."""
    config = {"max_charging_current": max_current, "locked": locked, "serial_number": "900001", "part_number": "PLP1-0-2-4-9-002-E", "software": {"currentVersion": "5.5.10"}}
    if eco is not None:
        config["ecosmart"] = eco
    return {"status_id": status_id, "charging_power": power, "added_energy": energy, "max_available_power": 32, "name": name, "config_data": config}


def test_normalise_charging():
    """A charging payload gives watts, session energy and the identity fields."""
    charger = normalise_charger(101, _status())
    assert charger.charger_id == "101"
    assert charger.name == "Garage" and charger.serial == "900001"
    assert charger.part_number == "PLP1-0-2-4-9-002-E" and charger.software_version == "5.5.10"
    assert charger.status == "Charging" and charger.status_id == 193
    assert charger.connected is True and charger.charging is True and charger.paused is False
    assert charger.power_w == 7200.0, charger.power_w
    assert charger.session_energy_kwh == 4.5
    assert charger.max_charging_current == 32 and charger.max_available_current == 32
    assert charger.locked is False and charger.eco_smart is None
    print("  ✓ A charging payload is normalised")


def test_normalise_status_table():
    """Every status code maps to the documented text, connected, charging and paused flags."""
    expected = {
        193: ("Charging", True, True, False), 194: ("Charging", True, True, False), 195: ("Charging", True, True, False),
        196: ("Discharging", True, False, False),
        178: ("Paused", True, False, True), 182: ("Paused", True, False, True),
        177: ("Scheduled", True, False, False), 179: ("Scheduled", True, False, False),
        164: ("Waiting", True, False, False),
        180: ("Waiting for car demand", True, False, False), 181: ("Waiting for car demand", True, False, False),
        183: ("Waiting in queue by Power Sharing", True, False, False), 184: ("Waiting in queue by Power Sharing", True, False, False),
        185: ("Waiting in queue by Power Boost", True, False, False), 186: ("Waiting in queue by Power Boost", True, False, False),
        187: ("Waiting MID failed", True, False, False), 188: ("Waiting MID safety margin exceeded", True, False, False),
        189: ("Waiting in queue by Eco-Smart", True, False, False),
        210: ("Locked, car connected", True, False, False),
        165: ("Locked", False, False, False), 209: ("Locked", False, False, False),
        161: ("Ready", False, False, False), 162: ("Ready", False, False, False),
        0: ("Disconnected", False, False, False), 163: ("Disconnected", False, False, False),
        166: ("Updating", False, False, False),
        14: ("Error", False, False, False), 15: ("Error", False, False, False),
    }
    assert set(expected) == set(WALLBOX_STATUS), set(expected) ^ set(WALLBOX_STATUS)
    for status_id, (text, connected, charging, paused) in expected.items():
        charger = normalise_charger(101, _status(status_id=status_id))
        assert (charger.status, charger.connected, charger.charging, charger.paused) == (text, connected, charging, paused), status_id
    unknown = normalise_charger(101, _status(status_id=999))
    assert (unknown.status, unknown.connected, unknown.charging, unknown.paused) == ("Unknown", False, False, False)
    print("  ✓ Every status code maps to the right state")


def test_normalise_eco_smart():
    """Eco-Smart is None when unsupported, off when disabled, otherwise the mode."""
    assert normalise_charger(101, _status()).eco_smart is None
    assert normalise_charger(101, _status(eco={"enabled": False, "mode": 0})).eco_smart == "off"
    assert normalise_charger(101, _status(eco={"enabled": True, "mode": 0})).eco_smart == "eco_mode"
    assert normalise_charger(101, _status(eco={"enabled": True, "mode": 1})).eco_smart == "full_solar"
    assert normalise_charger(101, _status(eco={"enabled": True})).eco_smart is None
    print("  ✓ Eco-Smart mode is normalised")


def test_normalise_handles_bad_values():
    """Null numbers, a missing config block and a non-dict payload give safe defaults, never an exception."""
    charger = normalise_charger(101, {"status_id": None, "charging_power": None, "added_energy": "junk", "config_data": None})
    assert charger.status == "Disconnected" and charger.connected is False
    assert charger.power_w == 0.0 and charger.session_energy_kwh == 0.0
    assert charger.max_charging_current == 0 and charger.max_available_current == 0
    assert charger.name == "Wallbox 101" and charger.serial == "" and charger.locked is False

    empty = normalise_charger(101, "not a dict")
    assert empty.status == "Disconnected" and empty.power_w == 0.0

    truthy_lock = normalise_charger(101, _status(locked=1))
    assert truthy_lock.locked is True
    print("  ✓ Bad values normalise to safe defaults")


def test_normalise_captured_payloads():
    """The payloads captured from a real charger normalise to the states they were captured in."""
    charging = normalise_charger(101, MOCK_STATUS_CHARGING)
    assert charging.charging is True and charging.power_w > 0
    ready = normalise_charger(101, MOCK_STATUS_READY)
    assert ready.connected is False and ready.power_w == 0.0
    paused = normalise_charger(101, MOCK_STATUS_PAUSED)
    assert paused.paused is True and paused.connected is True
    print("  ✓ Captured payloads normalise to their real states")
```

Add all five to `test_wallbox()`.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: `ImportError: cannot import name 'CHARGING_STATUS_IDS' from 'wallbox'`.

- [ ] **Step 3: Write the implementation**

Add `from dataclasses import dataclass` and `from typing import Optional` to the imports. Add above `class WallboxError`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Expected: five more `✓` lines, no traceback. If `test_normalise_captured_payloads` fails, the real payload differs from the spec: stop and report, do not bend the test.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): normalise charger status payloads"
```

---

### Task 4: Transport — control calls

**Files:**
- Modify: `apps/predbat/wallbox.py` (methods on `WallboxTransport`)
- Modify: `apps/predbat/tests/test_wallbox.py`

**Interfaces:**
- Consumes: `WallboxTransport._api(method, path, body=None, write=False)`, `ECO_SMART_OPTIONS`.
- Produces, all returning the response dict: `async pause(charger_id)`, `async resume(charger_id)`, `async resume_schedule(charger_id)`, `async set_locked(charger_id, locked: bool)`, `async set_max_charging_current(charger_id, amps: int)`, `async set_eco_smart(charger_id, mode: str)`.

- [ ] **Step 1: Write the failing tests**

Add `WallboxPermissionError` to the import list. Add:

```python
def _signed_in_transport():
    """A transport that already holds a valid token, so each test sees only the call it makes."""
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() + 3600
    return transport


def test_transport_control_requests():
    """Each control sends exactly the method, URL and body the Wallbox API expects."""
    eco_body = lambda enabled, mode: {"data": {"attributes": {"enabled": enabled, "mode": mode}, "type": "eco_smart"}}
    cases = [
        (lambda t: t.pause(101), "POST", "v3/chargers/101/remote-action", {"action": 2}),
        (lambda t: t.resume(101), "POST", "v3/chargers/101/remote-action", {"action": 1}),
        (lambda t: t.resume_schedule(101), "POST", "v3/chargers/101/remote-action", {"action": 9}),
        (lambda t: t.set_locked(101, True), "PUT", "v2/charger/101", {"locked": 1}),
        (lambda t: t.set_locked(101, False), "PUT", "v2/charger/101", {"locked": 0}),
        (lambda t: t.set_max_charging_current(101, 16), "PUT", "v2/charger/101", {"maxChargingCurrent": 16}),
        (lambda t: t.set_eco_smart(101, "off"), "PUT", "v4/chargers/101/eco-smart", eco_body(0, 0)),
        (lambda t: t.set_eco_smart(101, "eco_mode"), "PUT", "v4/chargers/101/eco-smart", eco_body(1, 0)),
        (lambda t: t.set_eco_smart(101, "full_solar"), "PUT", "v4/chargers/101/eco-smart", eco_body(1, 1)),
    ]
    for action, method, path, body in cases:
        session, calls = _session([_response({})])
        with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
            run_async(action(_signed_in_transport()))
        assert len(calls) == 1, calls
        assert (calls[0]["method"], calls[0]["url"], calls[0]["json"]) == (method, WALLBOX_API_URL + path, body), calls[0]
        assert calls[0]["headers"]["Content-Type"] == "application/json;charset=UTF-8"
    print("  ✓ Control calls send the right method, URL and body")


def test_transport_control_refused_for_rights():
    """A 403 on a write is a rights problem, not bad credentials, and is not retried behind a sign in."""
    session, calls = _session([_response({}, status=403)])
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        try:
            run_async(_signed_in_transport().set_locked(101, True))
            raise AssertionError("Expected WallboxPermissionError")
        except WallboxPermissionError:
            pass
    assert len(calls) == 1, "A rights refusal must not trigger a sign in and retry"
    print("  ✓ A 403 on a write raises WallboxPermissionError")


def test_transport_control_accepts_an_empty_body():
    """Eco-Smart answers with no JSON body; that is a success, not a decode error."""
    session, _ = _session([_response(json_error=ValueError("empty"))])
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        assert run_async(_signed_in_transport().set_eco_smart(101, "off")) == {}
    print("  ✓ A control call with an empty body succeeds")


def test_transport_rejects_an_unknown_eco_smart_mode():
    """An Eco-Smart mode that is not one of the three options never reaches the API."""
    session, calls = _session([])
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        try:
            run_async(_signed_in_transport().set_eco_smart(101, "turbo"))
            raise AssertionError("Expected WallboxApiError")
        except WallboxApiError:
            pass
    assert calls == []
    print("  ✓ An unknown Eco-Smart mode is rejected locally")
```

Add all four to `test_wallbox()`.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: `AttributeError: 'WallboxTransport' object has no attribute 'pause'`.

- [ ] **Step 3: Write the implementation**

Add next to the other constants:

```python
# remote-action codes, from the Wallbox portal
REMOTE_ACTION_RESUME = 1
REMOTE_ACTION_PAUSE = 2
REMOTE_ACTION_RESUME_SCHEDULE = 9
```

Add to `WallboxTransport`:

```python
    async def _remote_action(self, charger_id, action):
        """Send one remote-action code to a charger."""
        return await self._api("POST", "v3/chargers/{}/remote-action".format(charger_id), body={"action": action}, write=True)

    async def pause(self, charger_id):
        """Pause the charging session. Only has an effect while the charger is charging."""
        return await self._remote_action(charger_id, REMOTE_ACTION_PAUSE)

    async def resume(self, charger_id):
        """Resume a paused charging session. Has no effect on a Scheduled or Waiting charger."""
        return await self._remote_action(charger_id, REMOTE_ACTION_RESUME)

    async def resume_schedule(self, charger_id):
        """Hand the charger back to its own schedule and Eco-Smart mode after a manual stop."""
        return await self._remote_action(charger_id, REMOTE_ACTION_RESUME_SCHEDULE)

    async def set_locked(self, charger_id, locked):
        """Lock or unlock the charger."""
        return await self._api("PUT", "v2/charger/{}".format(charger_id), body={"locked": 1 if locked else 0}, write=True)

    async def set_max_charging_current(self, charger_id, amps):
        """Set the maximum charging current in amps."""
        return await self._api("PUT", "v2/charger/{}".format(charger_id), body={"maxChargingCurrent": int(amps)}, write=True)

    async def set_eco_smart(self, charger_id, mode):
        """Set the Eco-Smart solar charging mode to one of ECO_SMART_OPTIONS."""
        if mode not in ECO_SMART_OPTIONS:
            raise WallboxApiError("unknown Eco-Smart mode '{}'".format(mode))
        attributes = {"enabled": 0 if mode == ECO_SMART_OFF else 1, "mode": 1 if mode == ECO_SMART_FULL_SOLAR else 0}
        return await self._api("PUT", "v4/chargers/{}/eco-smart".format(charger_id), body={"data": {"attributes": attributes, "type": "eco_smart"}}, write=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Expected: four more `✓` lines.

- [ ] **Step 5: Extend the command line mode and commit**

Replace `run_wallbox_cli` and `main` with versions that take the control flags, so the same calls can be tried by hand:

```python
async def run_wallbox_cli(args):  # pragma: no cover
    """Sign in, list the chargers, print each one's status and optionally send one control."""
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
            print(normalise_charger(charger_id, payload))

    target = args.charger if args.charger is not None else charger_ids[0]
    action = None
    if args.pause:
        action = ("pause", transport.pause(target))
    elif args.resume:
        action = ("resume", transport.resume(target))
    elif args.resume_schedule:
        action = ("resume schedule", transport.resume_schedule(target))
    elif args.lock:
        action = ("lock", transport.set_locked(target, True))
    elif args.unlock:
        action = ("unlock", transport.set_locked(target, False))
    elif args.max_current is not None:
        action = ("set max current {}A".format(args.max_current), transport.set_max_charging_current(target, args.max_current))
    elif args.eco_smart:
        action = ("set Eco-Smart {}".format(args.eco_smart), transport.set_eco_smart(target, args.eco_smart))
    if not action:
        return
    print("\nSending {} to {}...".format(action[0], target))
    await action[1]
    print("Waiting {}s for the charger to report the change...".format(COMMAND_SETTLE_SECONDS))
    await asyncio.sleep(COMMAND_SETTLE_SECONDS)
    print(normalise_charger(target, await transport.get_status(target)))


def main():  # pragma: no cover
    """Main function for command line execution."""
    parser = argparse.ArgumentParser(description="Test the Wallbox API")
    parser.add_argument("--username", required=True, help="Wallbox account email address")
    parser.add_argument("--password", required=True, help="Wallbox account password")
    parser.add_argument("--raw", action="store_true", help="Print each charger's full status payload as JSON")
    parser.add_argument("--charger", default=None, help="Charger id to control; defaults to the first one")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pause", action="store_true", help="Pause the charging session")
    group.add_argument("--resume", action="store_true", help="Resume a paused charging session")
    group.add_argument("--resume-schedule", action="store_true", help="Hand the charger back to its own schedule")
    group.add_argument("--lock", action="store_true", help="Lock the charger")
    group.add_argument("--unlock", action="store_true", help="Unlock the charger")
    group.add_argument("--max-current", type=int, default=None, help="Set the maximum charging current in amps")
    group.add_argument("--eco-smart", choices=ECO_SMART_OPTIONS, default=None, help="Set the Eco-Smart mode")
    args = parser.parse_args()
    asyncio.run(run_wallbox_cli(args))
```

Add next to the constants:

```python
# How long the command line mode waits before reading a charger back after a control
COMMAND_SETTLE_SECONDS = 8
```

Run the test command once more (expected: still passing), then:

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): transport control calls and command line actions"
```

---

### Task 5: Component — registration, polling, publishing and error handling

**Files:**
- Modify: `apps/predbat/wallbox.py` (add `WallboxAPI`)
- Modify: `apps/predbat/components.py` (after the `"myenergi"` entry, about line 291)
- Modify: `apps/predbat/config.py` (after the `myenergi_*` keys, about line 2779)
- Modify: `apps/predbat/tests/test_wallbox.py`

**Interfaces:**
- Consumes: `WallboxTransport`, `normalise_charger`, `WallboxCharger`, the error classes.
- Produces on `WallboxAPI(ComponentBase)`:
  - `initialize(self, username, password, automatic=True, wallbox_control=False, poll_seconds=120)`
  - attributes `transport`, `chargers` (dict of `charger_id` str to `WallboxCharger`), `charger_ids` (list as the API returned them, sorted), `stale_ids` (set of str), `queued_events` (list), `poll_seconds`, `skip_cycles`, `backoff_cycles`, `control_active` (bool, `False` until Task 8), `control_enabled` (bool, `True`)
  - `entity_prefix(charger) -> str`, `ordered_chargers() -> list[WallboxCharger]` (sorted by `charger_id`, the order that makes charger N car N)
  - `async run(seconds, first) -> bool`, `async poll(seconds, first) -> bool`, `async process_events() -> bool`, `async publish_data()`
  - `warn_permission()` (logs the rights warning once)
  - hooks that are no-ops in this task and filled in later: `async load_control_state()`, `enable_control()`, `automatic_config()`, `async control_tick(now)`
- Test helpers produced: `_StubTransport`, `_make_component(statuses=None, **overrides)`.

- [ ] **Step 1: Write the failing tests**

Add `WallboxAPI`, `MIN_POLL_SECONDS`, `MAX_POLL_SECONDS` to the import list. Add:

```python
class _StubTransport:
    """In-memory transport: serves statuses by charger id and records every call."""

    def __init__(self, statuses=None):
        """Hold the statuses to serve; errors maps a call tuple to an exception to raise once."""
        self.statuses = statuses if statuses is not None else {101: _status()}
        self.calls = []
        self.errors = {}

    def _record(self, *call):
        """Record a call, raising the queued error for it if there is one."""
        self.calls.append(call)
        error = self.errors.pop(call, None) or self.errors.pop((call[0],), None)
        if error:
            raise error
        return {}

    async def list_chargers(self):
        """Return the ids of the statuses held."""
        self._record("list_chargers")
        return list(self.statuses)

    async def get_status(self, charger_id):
        """Return one charger's status payload."""
        self._record("get_status", charger_id)
        return self.statuses[charger_id]

    async def pause(self, charger_id):
        """Record a pause."""
        return self._record("pause", charger_id)

    async def resume(self, charger_id):
        """Record a resume."""
        return self._record("resume", charger_id)

    async def resume_schedule(self, charger_id):
        """Record a resume schedule."""
        return self._record("resume_schedule", charger_id)

    async def set_locked(self, charger_id, locked):
        """Record a lock change."""
        return self._record("set_locked", charger_id, locked)

    async def set_max_charging_current(self, charger_id, amps):
        """Record a current change."""
        return self._record("set_max_charging_current", charger_id, amps)

    async def set_eco_smart(self, charger_id, mode):
        """Record an Eco-Smart change."""
        return self._record("set_eco_smart", charger_id, mode)

    def count(self, name):
        """How many times a call of this name was made."""
        return len([call for call in self.calls if call[0] == name])


def _make_component(statuses=None, **overrides):
    """Build a WallboxAPI against MockBase with a stub transport and a captured log."""
    args = {"username": "user@example.com", "password": "secret", "automatic": True, "wallbox_control": False, "poll_seconds": 120}
    args.update(overrides)
    component = WallboxAPI(MockBase(), **args)
    component.transport = _StubTransport(statuses)
    component.log_messages = []
    component.log = component.log_messages.append
    return component


def _logged(component, text):
    """Was a message containing this text logged."""
    return any(text in message for message in component.log_messages)


def test_component_registration():
    """The component is registered with matching config keys and event filter."""
    import inspect

    from components import COMPONENT_LIST, load_component_class
    from config import APPS_SCHEMA

    entry = COMPONENT_LIST["wallbox"]
    assert load_component_class(entry) is WallboxAPI
    assert entry["event_filter"] == "predbat_wallbox_"
    assert entry["phase"] == 1 and entry["can_restart"] is True
    assert entry["args"]["username"]["required"] is True and entry["args"]["username"]["secret"] is True
    assert entry["args"]["password"]["required"] is True and entry["args"]["password"]["secret"] is True
    assert {spec["config"] for spec in entry["args"].values()} == {"wallbox_username", "wallbox_password", "wallbox_automatic", "wallbox_control", "wallbox_poll_seconds"}
    parameters = inspect.signature(WallboxAPI.initialize).parameters
    for arg_name, spec in entry["args"].items():
        assert arg_name in parameters, "initialize() has no parameter '{}'".format(arg_name)
        assert spec["config"] in APPS_SCHEMA, "{} missing from APPS_SCHEMA".format(spec["config"])
    print("  ✓ Component is registered with matching config keys")


def test_component_poll_seconds_is_clamped():
    """The poll interval is a whole number of minutes between the limits."""
    assert _make_component(poll_seconds=120).poll_seconds == 120
    assert _make_component(poll_seconds=90).poll_seconds == 120
    assert _make_component(poll_seconds=5).poll_seconds == MIN_POLL_SECONDS
    assert _make_component(poll_seconds=99999).poll_seconds == MAX_POLL_SECONDS
    assert _make_component(poll_seconds="junk").poll_seconds == 120
    print("  ✓ Poll interval is rounded and clamped")


def test_component_missing_credentials():
    """Without both credentials there is no transport and run() fails."""
    component = WallboxAPI(MockBase(), username=None, password="secret")
    assert component.transport is None
    assert run_async(component.run(0, True)) is False
    print("  ✓ Missing credentials leave the component failed")


def test_component_publishes_one_charger():
    """One charger without Eco-Smart publishes exactly the expected entities and values."""
    component = _make_component()
    assert run_async(component.run(0, True)) is True

    entities = component.base.entities
    prefix = "predbat_wallbox_101"
    wallbox_entities = {name for name in entities if "_wallbox_" in name}
    assert wallbox_entities == {
        "sensor.{}_status".format(prefix), "sensor.{}_power".format(prefix), "sensor.{}_session_energy".format(prefix),
        "binary_sensor.{}_connected".format(prefix), "binary_sensor.{}_charging".format(prefix),
        "switch.{}_charging".format(prefix), "switch.{}_locked".format(prefix), "number.{}_max_charging_current".format(prefix),
    }, wallbox_entities
    assert entities["sensor.{}_status".format(prefix)]["state"] == "Charging"
    status_attributes = entities["sensor.{}_status".format(prefix)]["attributes"]
    assert status_attributes["status_id"] == 193 and status_attributes["serial_number"] == "900001" and status_attributes["name"] == "Garage"
    assert entities["sensor.{}_power".format(prefix)]["state"] == 7200.0
    assert entities["sensor.{}_power".format(prefix)]["attributes"]["unit_of_measurement"] == "W"
    assert entities["sensor.{}_session_energy".format(prefix)]["state"] == 4.5
    assert entities["binary_sensor.{}_connected".format(prefix)]["state"] == "on"
    assert entities["binary_sensor.{}_charging".format(prefix)]["state"] == "on"
    assert entities["switch.{}_charging".format(prefix)]["state"] == "on"
    assert entities["switch.{}_locked".format(prefix)]["state"] == "off"
    number = entities["number.{}_max_charging_current".format(prefix)]
    assert number["state"] == 32 and number["attributes"]["min"] == 6 and number["attributes"]["max"] == 32 and number["attributes"]["step"] == 1
    assert component.last_success_timestamp is not None
    print("  ✓ One charger publishes the expected entities")


def test_component_publishes_two_chargers_and_eco_smart():
    """Each charger gets its own entities, and the Eco-Smart select appears only where supported."""
    component = _make_component({202: _status(status_id=161, power=0, energy=0), 101: _status(eco={"enabled": True, "mode": 1})})
    assert run_async(component.run(0, True)) is True

    entities = component.base.entities
    assert entities["sensor.predbat_wallbox_202_status"]["state"] == "Ready"
    assert entities["binary_sensor.predbat_wallbox_202_connected"]["state"] == "off"
    assert entities["switch.predbat_wallbox_202_charging"]["state"] == "off"
    select = entities["select.predbat_wallbox_101_eco_smart"]
    assert select["state"] == "full_solar" and select["attributes"]["options"] == ["off", "eco_mode", "full_solar"]
    assert "select.predbat_wallbox_202_eco_smart" not in entities
    assert [charger.charger_id for charger in component.ordered_chargers()] == ["101", "202"]
    print("  ✓ Two chargers publish separately, Eco-Smart only where supported")


def test_component_poll_cadence():
    """Status is polled on the first run and every poll_seconds; the charger list every 30 minutes."""
    component = _make_component()
    run_async(component.run(0, True))
    assert component.transport.count("get_status") == 1 and component.transport.count("list_chargers") == 1
    run_async(component.run(60, False))
    assert component.transport.count("get_status") == 1, "60s is between polls at poll_seconds=120"
    run_async(component.run(120, False))
    assert component.transport.count("get_status") == 2
    assert component.transport.count("list_chargers") == 1, "The charger list is not refetched every poll"
    run_async(component.run(1800, False))
    assert component.transport.count("list_chargers") == 2
    print("  ✓ Poll cadence follows poll_seconds and the 30 minute list refresh")


def test_component_no_chargers():
    """An account with no chargers warns on the first run and does not stamp a success."""
    component = _make_component({})
    assert run_async(component.run(0, True)) is True
    assert _logged(component, "no chargers were found")
    assert component.last_success_timestamp is None
    assert not [name for name in component.base.entities if "_wallbox_" in name]
    print("  ✓ An account with no chargers warns and publishes nothing")


def test_component_one_charger_failing_does_not_blank_the_other():
    """A status failure on one charger keeps the other published and keeps the charger order."""
    component = _make_component({101: _status(), 202: _status(status_id=161, power=0)})
    run_async(component.run(0, True))
    component.transport.errors[("get_status", 202)] = WallboxApiError("HTTP 500")
    component.transport.statuses[101] = _status(status_id=178, power=0)

    assert run_async(component.run(120, False)) is True
    assert component.base.entities["sensor.predbat_wallbox_101_status"]["state"] == "Paused"
    assert component.stale_ids == {"202"}
    assert [charger.charger_id for charger in component.ordered_chargers()] == ["101", "202"], "Charger N must stay car N"
    assert _logged(component, "could not read charger 202")

    assert run_async(component.run(240, False)) is True
    assert component.stale_ids == set()
    print("  ✓ One failing charger does not blank the other")


def test_component_every_charger_failing_fails_the_cycle():
    """When no charger could be read the cycle fails and no success is stamped."""
    component = _make_component()
    component.transport.errors[("get_status",)] = WallboxApiError("HTTP 500")
    assert run_async(component.run(0, True)) is False
    assert component.last_success_timestamp is None
    assert _logged(component, "poll failed")
    print("  ✓ A poll that reads no charger fails the cycle")


def test_component_auth_failure():
    """Bad credentials log an error naming the config keys and fail the cycle."""
    component = _make_component()
    component.transport.errors[("list_chargers",)] = WallboxAuthError("rejected")
    assert run_async(component.run(0, True)) is False
    assert _logged(component, "Error: wallbox:") and _logged(component, "wallbox_username")
    print("  ✓ Bad credentials are reported as an error")


def test_component_rate_limit_backoff():
    """A 429 skips polls on a doubling back-off capped at 15 minutes, then recovers."""
    component = _make_component()
    run_async(component.run(0, True))

    component.transport.errors[("get_status",)] = WallboxRateLimitError("429")
    assert run_async(component.run(120, False)) is True, "A rate limit after start-up is not a failed cycle"
    assert component.skip_cycles == 2 and _logged(component, "rate limited")

    polls = component.transport.count("get_status")
    run_async(component.run(180, False))
    run_async(component.run(240, False))
    assert component.transport.count("get_status") == polls, "Both skipped cycles made no call"

    for expected in (4, 8, 15, 15):
        component.skip_cycles = 0
        component.transport.errors[("get_status",)] = WallboxRateLimitError("429")
        run_async(component.run(120, False))
        assert component.skip_cycles == expected, (expected, component.skip_cycles)

    component.skip_cycles = 0
    assert run_async(component.run(120, False)) is True
    assert component.backoff_cycles == 0, "A good poll resets the back-off"
    print("  ✓ Rate limit back-off doubles, caps at 15 minutes and resets")


def test_component_rate_limit_on_first_run_fails():
    """A 429 on the very first run fails it, so the component is not marked started with no data."""
    component = _make_component()
    component.transport.errors[("list_chargers",)] = WallboxRateLimitError("429")
    assert run_async(component.run(0, True)) is False
    # The retry of the first run must poll, not be swallowed by the skip counter
    assert run_async(component.run(0, True)) is True
    assert "sensor.predbat_wallbox_101_status" in component.base.entities
    print("  ✓ A rate limit on the first run fails it and the retry polls")
```

Add all twelve to `test_wallbox()`.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: `ImportError: cannot import name 'WallboxAPI' from 'wallbox'`.

- [ ] **Step 3: Register the component**

Run `impact({target: "COMPONENT_LIST", direction: "upstream"})` and `impact({target: "APPS_SCHEMA", direction: "upstream"})` and report the result before editing; both are additive dict entries.

In `apps/predbat/components.py`, after the closing `},` of the `"myenergi"` entry:

```python
    "wallbox": {
        "class": "wallbox.WallboxAPI",
        "name": "Wallbox Charger",
        "event_filter": "predbat_wallbox_",
        "args": {
            "username": {"required": True, "secret": True, "config": "wallbox_username"},
            "password": {"required": True, "secret": True, "config": "wallbox_password"},
            "automatic": {"required": False, "config": "wallbox_automatic", "default": True},
            "wallbox_control": {"required": False, "config": "wallbox_control", "default": False},
            "poll_seconds": {"required": False, "config": "wallbox_poll_seconds", "default": 120},
        },
        "phase": 1,
        "can_restart": True,
    },
```

In `apps/predbat/config.py`, after `"myenergi_zappi_control": {"type": "boolean"},`:

```python
    "wallbox_username": {"type": "string", "empty": False},
    "wallbox_password": {"type": "string", "empty": False},
    "wallbox_automatic": {"type": "boolean"},
    "wallbox_control": {"type": "boolean"},
    "wallbox_poll_seconds": {"type": "integer", "zero": False},
```

- [ ] **Step 4: Write the component**

In `wallbox.py` add `from component_base import ComponentBase` to the imports, add these constants, and add the class above `run_wallbox_cli`:

```python
# components.py's is_alive() fails a component whose last successful update is more than
# 60 minutes old, so the poll interval is capped at half that
MIN_POLL_SECONDS = 60
MAX_POLL_SECONDS = 30 * 60
DEFAULT_POLL_SECONDS = 120
CHARGER_LIST_SECONDS = 30 * 60

# Rate limit back-off, in 60 second run cycles. The cap keeps recovery well inside the
# 60 minute health window.
RATE_LIMIT_MIN_CYCLES = 2
RATE_LIMIT_MAX_CYCLES = 15

MIN_CHARGING_CURRENT = 6
# Used as the upper bound only when the charger does not report one
DEFAULT_MAX_CHARGING_CURRENT = 32

wallbox_attribute_table = {
    "status": {"friendly_name": "Wallbox Status", "icon": "mdi:information-outline"},
    "power": {"friendly_name": "Wallbox Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "session_energy": {"friendly_name": "Wallbox Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    "connected": {"friendly_name": "Wallbox Car Connected", "icon": "mdi:ev-plug-type2"},
    "charging": {"friendly_name": "Wallbox Charging", "icon": "mdi:battery-charging"},
    "charging_switch": {"friendly_name": "Wallbox Charge", "icon": "mdi:ev-station"},
    "locked": {"friendly_name": "Wallbox Locked", "icon": "mdi:lock"},
    "max_charging_current": {"friendly_name": "Wallbox Maximum Charging Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "min": MIN_CHARGING_CURRENT, "step": 1},
    "eco_smart": {"friendly_name": "Wallbox Eco-Smart", "icon": "mdi:solar-power", "options": ECO_SMART_OPTIONS},
    "control": {"friendly_name": "Wallbox Charge Control", "icon": "mdi:ev-station"},
}


class WallboxAPI(ComponentBase):
    """Wallbox component: monitoring, controls and Predbat-led charging for Wallbox chargers."""

    def initialize(self, username, password, automatic=True, wallbox_control=False, poll_seconds=120):
        """Set up component state and the transport."""
        self.automatic = automatic
        self.wallbox_control = bool(wallbox_control)
        # ComponentBase.start() calls run() every 60 seconds, so the interval is a whole number of those
        self.poll_seconds = min(MAX_POLL_SECONDS, max(MIN_POLL_SECONDS, int(round(_to_float(poll_seconds, DEFAULT_POLL_SECONDS) / 60.0)) * 60))

        self.chargers = {}
        self.charger_ids = []
        self.stale_ids = set()
        self.queued_events = []
        self.skip_cycles = 0
        self.backoff_cycles = 0
        self.permission_warned = False
        self._auto_configured = False
        self.control_active = False
        self.control_enabled = True
        self.transport = None

        if not username or not password:
            self.log("Error: wallbox: wallbox_username and wallbox_password must both be set")
            return
        self.transport = WallboxTransport(self.log, username, password)

    def entity_prefix(self, charger):
        """Return the entity name prefix for a charger, e.g. predbat_wallbox_12345."""
        return "{}_wallbox_{}".format(self.prefix, charger.charger_id)

    def ordered_chargers(self):
        """The chargers in id order. This order is what makes charger N the same thing as car N."""
        return [self.chargers[charger_id] for charger_id in sorted(self.chargers)]

    def warn_permission(self):
        """Say once that the account cannot control the charger."""
        if not self.permission_warned:
            self.log("Warn: wallbox: Wallbox refused a control - the account needs admin rights over the charger. Monitoring continues")
            self.permission_warned = True

    async def load_control_state(self):
        """Restore saved control state. Filled in with plan-led control."""

    def enable_control(self):
        """Decide whether plan-led control can run. Filled in with plan-led control."""

    def automatic_config(self):
        """Wire the charger entities into Predbat's car inputs. Filled in with automatic configuration."""

    async def control_tick(self, now):
        """Run one cycle of plan-led control. Filled in with plan-led control."""

    async def run(self, seconds, first):
        """Process queued control events, then poll and publish."""
        if not self.transport:
            return False
        if first:
            await self.load_control_state()
            self.enable_control()
            self.skip_cycles = 0
        if self.skip_cycles > 0:
            self.skip_cycles -= 1
            return True
        try:
            refresh = await self.process_events()
            if first or refresh or (seconds % self.poll_seconds) == 0:
                return await self.poll(seconds, first)
            return True
        except WallboxRateLimitError:
            self.backoff_cycles = min(RATE_LIMIT_MAX_CYCLES, max(RATE_LIMIT_MIN_CYCLES, self.backoff_cycles * 2))
            self.skip_cycles = self.backoff_cycles
            self.log("Warn: wallbox: rate limited by the Wallbox API, pausing polling for {} minutes".format(self.backoff_cycles))
            # After start-up one rate limit is not a failure, but a first run that got no
            # data must not mark the component started
            return not first
        except WallboxAuthError as exc:
            self.log("Error: wallbox: sign in failed, check wallbox_username and wallbox_password: {}".format(exc))
            return False
        except WallboxError as exc:
            self.log("Warn: wallbox: poll failed: {}".format(exc))
            return False

    async def process_events(self):
        """Run every queued control. Returns True when at least one ran, so the caller polls afresh."""
        refresh = False
        while self.queued_events:
            handler, *event_args = self.queued_events[0]
            try:
                await handler(*event_args)
            except WallboxRateLimitError:
                # Left on the queue, so it is retried once the back-off has passed
                raise
            except WallboxPermissionError:
                self.warn_permission()
            except WallboxError as exc:
                self.log("Warn: wallbox: control failed: {}".format(exc))
            self.queued_events.pop(0)
            refresh = True
        return refresh

    async def poll(self, seconds, first):
        """Read every charger, publish, and run automatic configuration and control."""
        if first or not self.charger_ids or (seconds % CHARGER_LIST_SECONDS) == 0:
            self.charger_ids = sorted(await self.transport.list_chargers(), key=str)
        if not self.charger_ids:
            if first:
                self.log("Warn: wallbox: signed in but no chargers were found on the account")
            return True

        chargers = {}
        stale_ids = set()
        for charger_id in self.charger_ids:
            key = str(charger_id)
            try:
                chargers[key] = normalise_charger(charger_id, await self.transport.get_status(charger_id))
            except WallboxApiError as exc:
                self.log("Warn: wallbox: could not read charger {}: {}".format(charger_id, exc))
                # Keep the last record so the charger keeps its place in the car order,
                # marked stale so control does not act on old state
                if key in self.chargers:
                    chargers[key] = self.chargers[key]
                    stale_ids.add(key)
        if len(stale_ids) == len(chargers):
            raise WallboxApiError("no charger could be read")

        self.chargers = chargers
        self.stale_ids = stale_ids
        await self.publish_data()
        if self.automatic and not self._auto_configured:
            self.automatic_config()
            self._auto_configured = True
        try:
            await self.control_tick(self.now_utc_exact)
        except WallboxPermissionError:
            self.warn_permission()
        except WallboxApiError as exc:
            # Monitoring succeeded, so a refused control is a warning, not a failed cycle
            self.log("Warn: wallbox: charge control failed: {}".format(exc))
        self.backoff_cycles = 0
        self.update_success_timestamp()
        return True

    async def publish_data(self):
        """Publish every known charger as Predbat entities."""
        if self.control_active:
            # Published only while control could act on it, so the switch is never a lie
            self.dashboard_item("switch.{}_wallbox_control".format(self.prefix), state="on" if self.control_enabled else "off", attributes=wallbox_attribute_table["control"], app="wallbox")
        for charger in self.ordered_chargers():
            prefix = self.entity_prefix(charger)
            status_attributes = dict(wallbox_attribute_table["status"])
            status_attributes.update({"status_id": charger.status_id, "name": charger.name, "serial_number": charger.serial, "part_number": charger.part_number, "software_version": charger.software_version})
            current_attributes = dict(wallbox_attribute_table["max_charging_current"])
            current_attributes["max"] = charger.max_available_current if charger.max_available_current >= MIN_CHARGING_CURRENT else DEFAULT_MAX_CHARGING_CURRENT

            self.dashboard_item("sensor.{}_status".format(prefix), state=charger.status, attributes=status_attributes, app="wallbox")
            self.dashboard_item("sensor.{}_power".format(prefix), state=charger.power_w, attributes=wallbox_attribute_table["power"], app="wallbox")
            self.dashboard_item("sensor.{}_session_energy".format(prefix), state=charger.session_energy_kwh, attributes=wallbox_attribute_table["session_energy"], app="wallbox")
            self.dashboard_item("binary_sensor.{}_connected".format(prefix), state="on" if charger.connected else "off", attributes=wallbox_attribute_table["connected"], app="wallbox")
            self.dashboard_item("binary_sensor.{}_charging".format(prefix), state="on" if charger.charging else "off", attributes=wallbox_attribute_table["charging"], app="wallbox")
            self.dashboard_item("switch.{}_charging".format(prefix), state="on" if charger.charging else "off", attributes=wallbox_attribute_table["charging_switch"], app="wallbox")
            self.dashboard_item("switch.{}_locked".format(prefix), state="on" if charger.locked else "off", attributes=wallbox_attribute_table["locked"], app="wallbox")
            self.dashboard_item("number.{}_max_charging_current".format(prefix), state=charger.max_charging_current, attributes=current_attributes, app="wallbox")
            if charger.eco_smart is not None:
                self.dashboard_item("select.{}_eco_smart".format(prefix), state=charger.eco_smart, attributes=wallbox_attribute_table["eco_smart"], app="wallbox")
```

Note on `len(stale_ids) == len(chargers)`: a first poll where every status call fails leaves `chargers` empty and `stale_ids` empty, so `0 == 0` raises, which is the intended "nothing could be read" failure.

- [ ] **Step 5: Run the tests to verify they pass**

Expected: twelve more `✓` lines. Also run `./run_all --test components > /tmp/wallbox_components.log 2>&1; tail -20 /tmp/wallbox_components.log` (the quick-suite test that imports every registered component); expected: pass.

- [ ] **Step 6: Commit**

Run `detect_changes()` and confirm only `wallbox.py`, the test file, `COMPONENT_LIST` and `APPS_SCHEMA` are affected.

```bash
git add apps/predbat/wallbox.py apps/predbat/components.py apps/predbat/config.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): component with polling, publishing and rate limit back-off"
```

---

### Task 6: Manual controls

**Files:**
- Modify: `apps/predbat/wallbox.py` (`WallboxAPI`)
- Modify: `apps/predbat/tests/test_wallbox.py`

**Interfaces:**
- Consumes: `queued_events`, `process_events()`, `entity_prefix()`, the transport control methods, `ECO_SMART_OPTIONS`, `MIN_CHARGING_CURRENT`, `DEFAULT_MAX_CHARGING_CURRENT`.
- Produces: `charger_for_entity(entity_id) -> WallboxCharger | None`; `async switch_event(entity_id, service)`, `async number_event(entity_id, value)`, `async select_event(entity_id, value)` (queue only); `async switch_event_handler(entity_id, service)`, `async number_event_handler(entity_id, value)`, `async select_event_handler(entity_id, value)`. Task 8 adds a `_wallbox_control` branch at the top of `switch_event_handler`.

- [ ] **Step 1: Write the failing tests**

```python
def _started_component(statuses=None, **overrides):
    """A component that has completed its first poll, with the transport call log cleared."""
    component = _make_component(statuses, **overrides)
    run_async(component.run(0, True))
    component.transport.calls = []
    return component


def test_controls_queue_rather_than_call():
    """An event only queues; the API call happens in the next run(), followed by a fresh poll."""
    component = _started_component()
    run_async(component.switch_event("switch.predbat_wallbox_101_charging", "turn_off"))
    assert component.transport.calls == [] and len(component.queued_events) == 1

    run_async(component.run(60, False))
    assert component.transport.calls[0] == ("pause", 101)
    assert ("get_status", 101) in component.transport.calls, "A control is followed by a re-poll even between polls"
    assert component.queued_events == []
    print("  ✓ Controls are queued and followed by a re-poll")


def test_switch_controls():
    """The charging and lock switches map to resume/pause and lock/unlock."""
    cases = [
        ("switch.predbat_wallbox_101_charging", "turn_on", ("resume", 101)),
        ("switch.predbat_wallbox_101_charging", "turn_off", ("pause", 101)),
        ("switch.predbat_wallbox_101_locked", "turn_on", ("set_locked", 101, True)),
        ("switch.predbat_wallbox_101_locked", "turn_off", ("set_locked", 101, False)),
    ]
    for entity_id, service, expected in cases:
        component = _started_component()
        run_async(component.switch_event_handler(entity_id, service))
        assert component.transport.calls == [expected], (entity_id, service, component.transport.calls)

    component = _started_component()
    run_async(component.switch_event_handler("switch.predbat_wallbox_101_charging", "toggle"))
    run_async(component.switch_event_handler("switch.predbat_wallbox_999_charging", "turn_on"))
    run_async(component.switch_event_handler("switch.predbat_wallbox_101_something", "turn_on"))
    assert component.transport.calls == [], "Unknown services, chargers and entities send nothing"
    print("  ✓ Switch controls send the right calls")


def test_number_control_clamps_and_ignores_junk():
    """The charging current is clamped to 6..max available, and a non-number is ignored."""
    component = _started_component()
    run_async(component.number_event_handler("number.predbat_wallbox_101_max_charging_current", 16))
    run_async(component.number_event_handler("number.predbat_wallbox_101_max_charging_current", "20.0"))
    run_async(component.number_event_handler("number.predbat_wallbox_101_max_charging_current", 2))
    run_async(component.number_event_handler("number.predbat_wallbox_101_max_charging_current", 500))
    assert component.transport.calls == [("set_max_charging_current", 101, 16), ("set_max_charging_current", 101, 20), ("set_max_charging_current", 101, 6), ("set_max_charging_current", 101, 32)]

    component.transport.calls = []
    run_async(component.number_event_handler("number.predbat_wallbox_101_max_charging_current", "junk"))
    run_async(component.number_event_handler("number.predbat_wallbox_101_max_charging_current", None))
    run_async(component.number_event_handler("number.predbat_wallbox_101_other", 16))
    assert component.transport.calls == [], "Junk values and other number entities send nothing"
    print("  ✓ Charging current is clamped and junk is ignored")


def test_select_control():
    """Eco-Smart accepts only its three options, and only on a charger that supports it."""
    component = _started_component({101: _status(eco={"enabled": False, "mode": 0})})
    run_async(component.select_event_handler("select.predbat_wallbox_101_eco_smart", "full_solar"))
    run_async(component.select_event_handler("select.predbat_wallbox_101_eco_smart", "turbo"))
    assert component.transport.calls == [("set_eco_smart", 101, "full_solar")]

    unsupported = _started_component()
    run_async(unsupported.select_event_handler("select.predbat_wallbox_101_eco_smart", "eco_mode"))
    assert unsupported.transport.calls == []
    print("  ✓ Eco-Smart select sends only valid modes to chargers that support it")


def test_control_refused_for_rights_warns_once():
    """A rights refusal warns once across repeated attempts and does not fail the cycle."""
    component = _started_component()
    for _ in range(2):
        component.transport.errors[("set_locked", 101, True)] = WallboxPermissionError("403")
        run_async(component.switch_event("switch.predbat_wallbox_101_locked", "turn_on"))
        assert run_async(component.run(60, False)) is True
    assert len([message for message in component.log_messages if "admin rights" in message]) == 1
    print("  ✓ A rights refusal warns once and monitoring continues")


def test_control_failure_is_logged_and_not_retried():
    """An API failure on a control is logged, dropped from the queue, and the cycle still succeeds."""
    component = _started_component()
    component.transport.errors[("pause", 101)] = WallboxApiError("HTTP 500")
    run_async(component.switch_event("switch.predbat_wallbox_101_charging", "turn_off"))
    assert run_async(component.run(60, False)) is True
    assert _logged(component, "control failed") and component.queued_events == []
    print("  ✓ A failed control is logged and dropped")


def test_control_survives_a_rate_limit():
    """A control that hits the rate limit stays queued and is sent after the back-off."""
    component = _started_component()
    component.transport.errors[("pause", 101)] = WallboxRateLimitError("429")
    run_async(component.switch_event("switch.predbat_wallbox_101_charging", "turn_off"))
    run_async(component.run(60, False))
    assert len(component.queued_events) == 1 and component.skip_cycles == 2

    component.skip_cycles = 0
    component.transport.calls = []
    run_async(component.run(120, False))
    assert component.transport.calls[0] == ("pause", 101) and component.queued_events == []
    print("  ✓ A rate limited control is retried after the back-off")


def test_charger_for_entity_requires_a_whole_id():
    """Charger 10 must not claim charger 101's entities."""
    component = _started_component({10: _status(), 101: _status()})
    assert component.charger_for_entity("switch.predbat_wallbox_101_charging").charger_id == "101"
    assert component.charger_for_entity("switch.predbat_wallbox_10_charging").charger_id == "10"
    assert component.charger_for_entity("switch.predbat_wallbox_control") is None
    print("  ✓ Entities resolve to a whole charger id")
```

Add all eight to `test_wallbox()`.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: `AttributeError: 'WallboxAPI' object has no attribute 'switch_event_handler'` (the base class `switch_event` is a no-op, so the first test fails on its queue assertion).

- [ ] **Step 3: Write the implementation**

Add to `WallboxAPI`:

```python
    def charger_for_entity(self, entity_id):
        """Find the charger an entity belongs to, or None.

        The trailing underscore anchors the match to a whole id, so charger 10 cannot
        claim charger 101's entities.
        """
        for charger in self.chargers.values():
            if "{}_".format(self.entity_prefix(charger)) in entity_id:
                return charger
        return None

    def api_id(self, charger):
        """The id in the form the API listed it, for a normalised charger."""
        for charger_id in self.charger_ids:
            if str(charger_id) == charger.charger_id:
                return charger_id
        return charger.charger_id

    async def switch_event(self, entity_id, service):
        """Queue a switch service call for the run loop."""
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    async def number_event(self, entity_id, value):
        """Queue a number change for the run loop."""
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def select_event(self, entity_id, value):
        """Queue a select change for the run loop."""
        self.queued_events.append((self.select_event_handler, entity_id, value))

    async def switch_event_handler(self, entity_id, service):
        """Pause/resume or lock/unlock in response to a charger switch."""
        if service not in ("turn_on", "turn_off"):
            return
        charger = self.charger_for_entity(entity_id)
        if not charger:
            return
        turn_on = service == "turn_on"
        if entity_id.endswith("_charging"):
            self.log("Info: wallbox: {} charging on {}".format("resuming" if turn_on else "pausing", charger.name))
            if turn_on:
                await self.transport.resume(self.api_id(charger))
            else:
                await self.transport.pause(self.api_id(charger))
        elif entity_id.endswith("_locked"):
            self.log("Info: wallbox: {} {}".format("locking" if turn_on else "unlocking", charger.name))
            await self.transport.set_locked(self.api_id(charger), turn_on)

    async def number_event_handler(self, entity_id, value):
        """Set the maximum charging current, clamped to what the charger allows."""
        if not entity_id.endswith("_max_charging_current"):
            return
        charger = self.charger_for_entity(entity_id)
        if not charger:
            return
        try:
            amps = int(float(value))
        except (TypeError, ValueError):
            self.log("Warn: wallbox: ignoring charging current '{}' for {}".format(value, charger.name))
            return
        upper = charger.max_available_current if charger.max_available_current >= MIN_CHARGING_CURRENT else DEFAULT_MAX_CHARGING_CURRENT
        amps = max(MIN_CHARGING_CURRENT, min(upper, amps))
        self.log("Info: wallbox: setting {} maximum charging current to {}A".format(charger.name, amps))
        await self.transport.set_max_charging_current(self.api_id(charger), amps)

    async def select_event_handler(self, entity_id, value):
        """Set the Eco-Smart mode on a charger that supports it."""
        if not entity_id.endswith("_eco_smart") or value not in ECO_SMART_OPTIONS:
            return
        charger = self.charger_for_entity(entity_id)
        if not charger or charger.eco_smart is None:
            return
        self.log("Info: wallbox: setting {} Eco-Smart to {}".format(charger.name, value))
        await self.transport.set_eco_smart(self.api_id(charger), value)
```

`api_id()` exists because the API lists ids as integers while entity names carry strings; calls go out with the id exactly as Wallbox listed it. The tests assert integer ids in the recorded calls, which pins this.

- [ ] **Step 4: Run the tests to verify they pass**

Expected: eight more `✓` lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): pause, lock, charging current and Eco-Smart controls"
```

---

### Task 7: Automatic configuration

**Files:**
- Modify: `apps/predbat/wallbox.py` (`WallboxAPI.automatic_config`)
- Modify: `apps/predbat/tests/test_wallbox.py`

**Interfaces:**
- Consumes: `ordered_chargers()`, `entity_prefix()`, `ComponentBase.set_arg_auto(arg, value, overwrite=True)`, `set_arg(arg, value)`, `get_arg(arg, default)`.
- Produces: `automatic_config()` (sync), called once by `poll()` when `self.automatic` is on.

- [ ] **Step 1: Write the failing tests**

```python
def test_automatic_config_two_chargers():
    """Each argument is a per-car list in charger id order, and num_cars is raised to match."""
    component = _make_component({202: _status(), 101: _status()})
    run_async(component.run(0, True))
    args = component.base.args
    assert args["car_charging_energy"] == ["sensor.predbat_wallbox_101_session_energy", "sensor.predbat_wallbox_202_session_energy"]
    assert args["car_charging_planned"] == ["binary_sensor.predbat_wallbox_101_connected", "binary_sensor.predbat_wallbox_202_connected"]
    assert args["car_charging_power"] == ["sensor.predbat_wallbox_101_power", "sensor.predbat_wallbox_202_power"]
    assert args["car_charging_now"] == ["sensor.predbat_wallbox_101_power", "sensor.predbat_wallbox_202_power"]
    assert args["num_cars"] == 2
    assert "car_charging_soc" not in args, "A Type 2 charger cannot report state of charge"
    print("  ✓ Automatic configuration wires per-car lists in charger order")


def test_automatic_config_single_charger_is_still_a_list():
    """One charger still produces lists, which is what the per-car arguments expect."""
    component = _make_component()
    run_async(component.run(0, True))
    assert component.base.args["car_charging_energy"] == ["sensor.predbat_wallbox_101_session_energy"]
    assert component.base.args["num_cars"] == 1
    print("  ✓ A single charger is still wired as a list")


def test_automatic_config_keeps_user_values():
    """A user's car_charging_now is kept, and a larger num_cars is not reduced."""
    component = _make_component()
    component.base.args["car_charging_now"] = ["binary_sensor.my_car_charging"]
    component.base.args["num_cars"] = 3
    run_async(component.run(0, True))
    assert component.base.args["car_charging_now"] == ["binary_sensor.my_car_charging"]
    assert component.base.args["num_cars"] == 3
    print("  ✓ User car_charging_now and a larger num_cars are kept")


def test_automatic_config_disabled():
    """With wallbox_automatic off nothing is wired."""
    component = _make_component(automatic=False)
    run_async(component.run(0, True))
    assert "car_charging_energy" not in component.base.args and "num_cars" not in component.base.args
    print("  ✓ Automatic configuration off wires nothing")


def test_automatic_config_runs_once():
    """A value the user changes after start-up is not put back by the next poll."""
    component = _make_component()
    run_async(component.run(0, True))
    component.base.args["car_charging_energy"] = ["sensor.something_else"]
    run_async(component.run(120, False))
    assert component.base.args["car_charging_energy"] == ["sensor.something_else"]
    print("  ✓ Automatic configuration runs once")
```

Add all five to `test_wallbox()`.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: `KeyError: 'car_charging_energy'`.

- [ ] **Step 3: Write the implementation**

Replace the placeholder `automatic_config` in `WallboxAPI`:

```python
    def automatic_config(self):
        """Wire the charger entities into Predbat's car charging inputs.

        Each argument is a per-car list in charger id order, so charger N is car N.
        car_charging_now takes the power sensors, which count as charging from
        CAR_CHARGING_NOW_POWER_W; a value the user set in apps.yaml is kept. Session
        energy resets with each session, which the shared incrementing-sensor handling
        already copes with. car_charging_soc is not set: a Type 2 connector cannot
        report the car's state of charge.
        """
        chargers = self.ordered_chargers()
        if not chargers:
            return
        energy_entities = ["sensor.{}_session_energy".format(self.entity_prefix(charger)) for charger in chargers]
        planned_entities = ["binary_sensor.{}_connected".format(self.entity_prefix(charger)) for charger in chargers]
        power_entities = ["sensor.{}_power".format(self.entity_prefix(charger)) for charger in chargers]

        self.log("Info: wallbox: setting car_charging_energy to {}".format(energy_entities))
        self.set_arg_auto("car_charging_energy", energy_entities)
        self.log("Info: wallbox: setting car_charging_planned to {}".format(planned_entities))
        self.set_arg_auto("car_charging_planned", planned_entities)
        self.log("Info: wallbox: setting car_charging_power and car_charging_now to {}".format(power_entities))
        self.set_arg_auto("car_charging_power", power_entities)
        self.set_arg_auto("car_charging_now", power_entities, overwrite=False)
        if _to_int(self.get_arg("num_cars", 0), 0) < len(chargers):
            self.log("Info: wallbox: setting num_cars to {}".format(len(chargers)))
            self.set_arg("num_cars", len(chargers))
```

- [ ] **Step 4: Run the tests to verify they pass**

Expected: five more `✓` lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): automatic car charging configuration"
```

---

### Task 8: Predbat-led charging

**Files:**
- Modify: `apps/predbat/wallbox.py` (`WallboxAPI`)
- Modify: `apps/predbat/tests/test_wallbox.py`

**Interfaces:**
- Consumes: `utils.parse_car_plan_windows(planned, now, local_tz)`, `utils.in_car_plan_window(windows, now)`, `ComponentBase.storage` (`async save(module, filename, data)`, `async load(module, filename)`, or `None`), `ordered_chargers()`, `stale_ids`, `api_id()`, `transport.pause/resume/resume_schedule`.
- Produces: `enable_control()`, `async load_control_state()`, `async save_control_state()`, `control_read_only_now() -> bool`, `refresh_car_windows(now) -> bool`, `async control_tick(now)`, `async control_charge(now)`, `async release_chargers()`; attributes `paused_by_predbat` (set of charger id str), `control_windows` (dict of car number to windows), `lock_warned` (set of charger id str); a `_wallbox_control` branch in `switch_event_handler`.

- [ ] **Step 1: Write the failing tests**

```python
CONTROL_TZ = pytz.timezone("Europe/London")
CONTROL_NOW = CONTROL_TZ.localize(datetime.datetime(2026, 10, 5, 23, 30))


def _plan_window(start, end):
    """Build one planned-window dict in the shape output.py publishes."""
    return {"start": start.strftime("%m-%d %H:%M:%S"), "end": end.strftime("%m-%d %H:%M:%S")}


# CONTROL_NOW is inside this window...
PLAN_INSIDE = [_plan_window(datetime.datetime(2026, 10, 5, 23, 0), datetime.datetime(2026, 10, 6, 1, 0))]
# ...and before this one
PLAN_OUTSIDE = [_plan_window(datetime.datetime(2026, 10, 6, 2, 0), datetime.datetime(2026, 10, 6, 4, 0))]


class _Storage:
    """Minimal in-memory stand-in for the Storage component."""

    def __init__(self):
        """Start empty."""
        self.data = {}

    async def save(self, module, filename, data, **kwargs):
        """Store a document."""
        self.data[(module, filename)] = data

    async def load(self, module, filename):
        """Return a stored document, or None."""
        return self.data.get((module, filename))


class _Components:
    """Stand-in for the component registry, serving only the storage component."""

    def __init__(self, storage):
        """Hold the storage stand-in to serve."""
        self.storage = storage

    def get_component(self, name):
        """Return the storage stand-in, and nothing else."""
        return self.storage if name == "storage" else None


def _control_component(plans, statuses=None, storage=None, **overrides):
    """A component with control on, chargers loaded and car plans published.

    plans maps car number to a list of _plan_window() dicts. The chargers are loaded
    directly rather than through run(), so each test calls control_tick() with a fixed clock.
    """
    overrides.setdefault("wallbox_control", True)
    component = _make_component(statuses, **overrides)
    component.local_tz = CONTROL_TZ
    component.base.local_tz = CONTROL_TZ
    if storage is not None:
        component.base.components = _Components(storage)
    for car_n, windows in plans.items():
        postfix = "" if car_n == 0 else "_{}".format(car_n)
        component.base.set_state_wrapper("binary_sensor.predbat_car_charging_slot" + postfix, "off", {"planned": windows})
    run_async(component.load_control_state())
    component.enable_control()
    _load_chargers(component)
    return component


def _load_chargers(component):
    """Load the stub transport's current statuses into the component, as a poll would."""
    component.charger_ids = sorted(component.transport.statuses, key=str)
    component.chargers = {str(charger_id): normalise_charger(charger_id, payload) for charger_id, payload in component.transport.statuses.items()}
    component.stale_ids = set()
    component.transport.calls = []


def test_control_needs_automatic():
    """Control stays off, with a warning, unless automatic configuration is on."""
    assert _control_component({}).control_active is True
    without_auto = _control_component({}, automatic=False)
    assert without_auto.control_active is False and _logged(without_auto, "needs wallbox_automatic")
    assert _control_component({}, wallbox_control=False).control_active is False
    print("  ✓ Control needs wallbox_control and wallbox_automatic")


def test_control_resumes_inside_a_window():
    """A Paused charger inside its car's window is resumed."""
    component = _control_component({0: PLAN_INSIDE}, {101: _status(status_id=178, power=0)})
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == [("resume", 101)]
    print("  ✓ A paused charger inside a window is resumed")


def test_control_pauses_outside_a_window():
    """A Charging charger outside its car's window is paused, and Predbat remembers it did so."""
    component = _control_component({0: PLAN_OUTSIDE})
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == [("pause", 101)]
    assert component.paused_by_predbat == {"101"}
    print("  ✓ A charging charger outside a window is paused")


def test_control_leaves_other_states_alone():
    """Scheduled, Waiting, Ready, Locked and already-correct chargers are sent nothing."""
    for plan, status_id in [(PLAN_INSIDE, 177), (PLAN_INSIDE, 179), (PLAN_INSIDE, 180), (PLAN_INSIDE, 164), (PLAN_INSIDE, 161), (PLAN_INSIDE, 193), (PLAN_INSIDE, 210), (PLAN_OUTSIDE, 178), (PLAN_OUTSIDE, 177), (PLAN_OUTSIDE, 161), (PLAN_OUTSIDE, 210)]:
        component = _control_component({0: plan}, {101: _status(status_id=status_id, power=0)})
        run_async(component.control_tick(CONTROL_NOW))
        assert component.transport.calls == [], (status_id, component.transport.calls)
    print("  ✓ Scheduled, Waiting, Ready and Locked chargers are left alone")


def test_control_warns_once_about_a_locked_charger():
    """A locked charger with a car connected inside a window is warned about once, never unlocked."""
    component = _control_component({0: PLAN_INSIDE}, {101: _status(status_id=210, power=0, locked=True)})
    run_async(component.control_tick(CONTROL_NOW))
    run_async(component.control_tick(CONTROL_NOW))
    assert len([message for message in component.log_messages if "is locked" in message]) == 1
    assert component.transport.count("set_locked") == 0
    print("  ✓ A locked charger is warned about once and never unlocked")


def test_control_is_per_car():
    """Charger N follows car N's plan: the second charger reads the _1 slot sensor."""
    statuses = {101: _status(status_id=178, power=0), 202: _status(status_id=193)}
    component = _control_component({0: PLAN_INSIDE, 1: PLAN_OUTSIDE}, statuses)
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == [("resume", 101), ("pause", 202)]
    print("  ✓ Each charger follows its own car's plan")


def test_control_skips_a_charger_with_no_plan():
    """A charger whose car has no slot sensor is left alone, not paused as if outside a window."""
    statuses = {101: _status(status_id=193), 202: _status(status_id=193)}
    component = _control_component({0: PLAN_INSIDE}, statuses)
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == [], "Charger 202 has no car 1 plan, so it must not be paused"

    nothing_planned = _control_component({})
    run_async(nothing_planned.control_tick(CONTROL_NOW))
    assert nothing_planned.transport.calls == [], "Before Predbat has planned anything, no charger is touched"
    print("  ✓ A charger with no car plan is left alone")


def test_control_skips_a_stale_charger():
    """A charger whose last status read failed is not controlled on old state."""
    component = _control_component({0: PLAN_OUTSIDE})
    component.stale_ids = {"101"}
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == []
    print("  ✓ A stale charger is not controlled")


def test_control_forgets_an_unplugged_charger():
    """Once the car is unplugged, Predbat no longer counts the charger as one it paused."""
    component = _control_component({0: PLAN_OUTSIDE})
    run_async(component.control_tick(CONTROL_NOW))
    assert component.paused_by_predbat == {"101"}
    component.transport.statuses[101] = _status(status_id=161, power=0)
    _load_chargers(component)
    run_async(component.control_tick(CONTROL_NOW))
    assert component.paused_by_predbat == set() and component.transport.calls == []
    print("  ✓ An unplugged charger is forgotten")


def test_control_releases_on_read_only_and_switch_off():
    """Read only mode and the control switch both resume what Predbat paused and hand back the schedule."""
    for release in ("read_only", "switch"):
        component = _control_component({0: PLAN_OUTSIDE})
        run_async(component.control_tick(CONTROL_NOW))
        component.transport.statuses[101] = _status(status_id=178, power=0)
        _load_chargers(component)

        if release == "read_only":
            component.base.args["set_read_only"] = True
        else:
            run_async(component.switch_event_handler("switch.predbat_wallbox_control", "turn_off"))
            assert component.control_enabled is False
        run_async(component.control_tick(CONTROL_NOW))
        assert component.transport.calls == [("resume", 101), ("resume_schedule", 101)], (release, component.transport.calls)
        assert component.paused_by_predbat == set()

        component.transport.calls = []
        run_async(component.control_tick(CONTROL_NOW))
        assert component.transport.calls == [], "Release happens once, and control stays quiet while released"
    print("  ✓ Read only mode and the control switch release the chargers")


def test_control_does_not_release_a_charger_it_did_not_pause():
    """A charger the user paused by hand is not resumed when Predbat releases."""
    component = _control_component({0: PLAN_OUTSIDE}, {101: _status(status_id=178, power=0)})
    component.base.args["set_read_only"] = True
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == []
    print("  ✓ A charger Predbat did not pause is not resumed on release")


def test_control_state_survives_a_restart():
    """After a restart with control turned off, the charger Predbat paused is still released."""
    storage = _Storage()
    component = _control_component({0: PLAN_OUTSIDE}, storage=storage)
    run_async(component.control_tick(CONTROL_NOW))
    assert storage.data[("wallbox", "control_state")] == {"control_enabled": True, "paused": ["101"]}

    restarted = _control_component({0: PLAN_OUTSIDE}, {101: _status(status_id=178, power=0)}, storage=storage, wallbox_control=False)
    assert restarted.control_active is False and restarted.paused_by_predbat == {"101"}
    run_async(restarted.control_tick(CONTROL_NOW))
    assert restarted.transport.calls == [("resume", 101), ("resume_schedule", 101)]
    assert storage.data[("wallbox", "control_state")]["paused"] == []
    print("  ✓ A restart with control off still releases the charger")


def test_control_switch_state_survives_a_restart():
    """The control switch stays off across a restart and is published in that state from the start."""
    storage = _Storage()
    component = _control_component({0: PLAN_OUTSIDE}, storage=storage)
    run_async(component.switch_event_handler("switch.predbat_wallbox_control", "turn_off"))

    restarted = _make_component(wallbox_control=True)
    restarted.base.components = _Components(storage)
    run_async(restarted.run(0, True))
    assert restarted.control_enabled is False
    assert restarted.base.entities["switch.predbat_wallbox_control"]["state"] == "off"
    print("  ✓ The control switch state survives a restart")


def test_control_switch_is_published_only_when_available():
    """The control switch appears only when control can actually run."""
    on = _make_component(wallbox_control=True)
    run_async(on.run(0, True))
    assert on.base.entities["switch.predbat_wallbox_control"]["state"] == "on"
    off = _make_component(wallbox_control=False)
    run_async(off.run(0, True))
    assert "switch.predbat_wallbox_control" not in off.base.entities
    print("  ✓ The control switch is published only when control is available")


def test_control_without_storage_still_works():
    """With no Storage component, control runs and nothing is raised."""
    component = _control_component({0: PLAN_OUTSIDE})
    assert component.storage is None
    run_async(component.control_tick(CONTROL_NOW))
    assert component.transport.calls == [("pause", 101)]
    print("  ✓ Control works without the Storage component")


def test_control_failure_does_not_fail_the_cycle():
    """A refused pause during a poll is a warning; monitoring still succeeds and it is retried next poll."""
    component = _make_component(wallbox_control=True)
    component.local_tz = CONTROL_TZ
    component.base.local_tz = CONTROL_TZ
    component.base.set_state_wrapper("binary_sensor.predbat_car_charging_slot", "off", {"planned": []})
    component.transport.errors[("pause", 101)] = WallboxApiError("HTTP 500")
    assert run_async(component.run(0, True)) is True
    assert _logged(component, "charge control failed") and component.paused_by_predbat == set()
    run_async(component.run(120, False))
    assert component.paused_by_predbat == {"101"}
    print("  ✓ A refused control is a warning and is retried")
```

Add all sixteen to `test_wallbox()`.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: `AssertionError` in `test_control_needs_automatic` (the placeholder `enable_control` never sets `control_active`).

- [ ] **Step 3: Write the implementation**

Add `from utils import parse_car_plan_windows, in_car_plan_window` to the imports and these constants:

```python
WALLBOX_STORAGE_MODULE = "wallbox"
WALLBOX_CONTROL_STATE = "control_state"
```

In `initialize`, next to `self.control_active = False`, add:

```python
        self.paused_by_predbat = set()
        self.control_windows = {}
        self.lock_warned = set()
        self.control_state_loaded = False
```

Replace the three placeholders (`load_control_state`, `enable_control`, `control_tick`) and add the rest:

```python
    def enable_control(self):
        """Decide whether Predbat-led charging can run, and say why when it cannot.

        Control needs automatic configuration because a charger is driven from its own
        car's plan, and it is automatic configuration that makes charger N car N.
        """
        self.control_active = False
        if not self.wallbox_control:
            return
        if not self.automatic:
            self.log("Warn: wallbox: wallbox_control needs wallbox_automatic to map each charger to a car, charge control is disabled")
            return
        self.control_active = True
        self.log("Info: wallbox: Predbat-led charge control enabled")

    async def load_control_state(self):
        """Restore the control switch and the list of chargers Predbat paused.

        The paused list is what lets a restart with control turned off still release a
        charger an earlier session left paused. Fails soft with no Storage component.
        """
        if self.control_state_loaded or self.storage is None:
            return
        try:
            saved = await self.storage.load(WALLBOX_STORAGE_MODULE, WALLBOX_CONTROL_STATE)
        except Exception as exc:
            self.log("Warn: wallbox: could not read the saved charge control state: {}".format(exc))
            return
        self.control_state_loaded = True
        if not isinstance(saved, dict):
            return
        if "control_enabled" in saved:
            self.control_enabled = bool(saved["control_enabled"])
        paused = saved.get("paused")
        if isinstance(paused, list):
            self.paused_by_predbat = {str(charger_id) for charger_id in paused}

    async def save_control_state(self):
        """Persist the control switch and the list of chargers Predbat paused."""
        if self.storage is None:
            return
        try:
            await self.storage.save(WALLBOX_STORAGE_MODULE, WALLBOX_CONTROL_STATE, {"control_enabled": self.control_enabled, "paused": sorted(self.paused_by_predbat)})
        except Exception as exc:
            self.log("Warn: wallbox: could not save the charge control state: {}".format(exc))

    def control_read_only_now(self):
        """Is Predbat in read only mode - the live attribute first, then the config argument."""
        read_only = getattr(self.base, "set_read_only", None)
        if read_only is None:
            read_only = self.get_arg("set_read_only", False)
        return bool(read_only)

    def refresh_car_windows(self, now):
        """Read each car's planned charging windows into control_windows.

        A car with no slot sensor yet is simply absent, so its charger is left alone.
        Returns True once at least one car's plan has been read.
        """
        windows = {}
        for car_n in range(len(self.chargers)):
            postfix = "" if car_n == 0 else "_{}".format(car_n)
            planned = self.get_state_wrapper("binary_sensor.{}_car_charging_slot{}".format(self.prefix, postfix), attribute="planned")
            if planned is None:
                continue
            windows[car_n] = parse_car_plan_windows(planned, now, self.local_tz)
        self.control_windows = windows
        return bool(windows)

    async def control_tick(self, now):
        """Run one cycle of charge control, releasing rather than just going quiet.

        Called on every poll whether or not control is available, because a charger a
        previous session paused must be released even when control has since been turned off.
        """
        reason = None
        if not self.control_active:
            reason = "charge control is not enabled"
        elif self.control_read_only_now():
            reason = "Predbat is in read only mode"
        elif not self.control_enabled:
            reason = "the charge control switch is off"
        if reason:
            if self.paused_by_predbat:
                self.log("Info: wallbox: releasing the chargers because {}".format(reason))
                await self.release_chargers()
            return
        await self.control_charge(now)

    async def release_chargers(self):
        """Resume every charger Predbat paused and hand each back to its own schedule.

        A charger that is no longer paused - resumed by hand, or unplugged - is only
        forgotten. The record is saved even if a call fails part way, so what was
        released stays released and the rest is retried on the next poll.
        """
        try:
            for charger_id in sorted(self.paused_by_predbat):
                charger = self.chargers.get(charger_id)
                if charger and charger_id in self.stale_ids:
                    continue
                if charger and charger.paused:
                    self.log("Info: wallbox: releasing {}".format(charger.name))
                    await self.transport.resume(self.api_id(charger))
                    await self.transport.resume_schedule(self.api_id(charger))
                self.paused_by_predbat.discard(charger_id)
        finally:
            await self.save_control_state()

    async def control_charge(self, now):
        """Drive each charger from its car's plan: resume a paused one inside a window, pause a charging one outside.

        Nothing else is sent. A Scheduled charger is following its own schedule and a
        Waiting one is waiting on the car, and resume does nothing for either.
        """
        if not self.refresh_car_windows(now):
            return
        before = set(self.paused_by_predbat)
        try:
            for car_n, charger in enumerate(self.ordered_chargers()):
                if car_n not in self.control_windows or charger.charger_id in self.stale_ids:
                    continue
                wanted = in_car_plan_window(self.control_windows[car_n], now)
                if not charger.connected:
                    self.paused_by_predbat.discard(charger.charger_id)
                    continue
                if wanted and charger.locked:
                    if charger.charger_id not in self.lock_warned:
                        self.log("Warn: wallbox: {} is locked, so it will not charge in its planned window. Predbat does not unlock chargers".format(charger.name))
                        self.lock_warned.add(charger.charger_id)
                    continue
                self.lock_warned.discard(charger.charger_id)
                if wanted and charger.paused:
                    self.log("Info: wallbox: resuming {} for car {}".format(charger.name, car_n))
                    await self.transport.resume(self.api_id(charger))
                    self.paused_by_predbat.discard(charger.charger_id)
                elif not wanted and charger.charging:
                    self.log("Info: wallbox: pausing {} for car {}".format(charger.name, car_n))
                    await self.transport.pause(self.api_id(charger))
                    self.paused_by_predbat.add(charger.charger_id)
        finally:
            if self.paused_by_predbat != before:
                await self.save_control_state()
```

In `switch_event_handler`, insert this between the `if service not in ("turn_on", "turn_off"): return` check and the `charger = self.charger_for_entity(entity_id)` line. It must come before the charger lookup, because the control switch belongs to no charger:

```python
        if entity_id.endswith("_wallbox_control"):
            self.control_enabled = service == "turn_on"
            self.log("Info: wallbox: charge control switched {}".format("on" if self.control_enabled else "off"))
            await self.save_control_state()
            return
```

Why `control_enabled` keeps a charger paused by the user out of release: `release_chargers` only walks `paused_by_predbat`, which is added to only where Predbat itself sent the pause.

- [ ] **Step 4: Run the tests to verify they pass**

Expected: sixteen more `✓` lines, and every earlier test still passing.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/wallbox.py apps/predbat/tests/test_wallbox.py
git commit -m "feat(wallbox): Predbat-led charging with persistent release"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/components.md`, `docs/car-charging.md`, `docs/apps-yaml.md`, `docs/devices.md`, `apps/predbat/config/apps.yaml`, `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`

No code, so no test cycle. The check is `./run_pre_commit` (markdown lint and British English spelling).

- [ ] **Step 1: `docs/components.md`**

Add `- [Wallbox Charger (wallbox)](#wallbox-charger-wallbox)` to the table of contents after the myenergi line (about line 20). After the end of the `### myenergi (myenergi)` section, add:

````markdown
### Wallbox Charger (wallbox)

Talks directly to the Wallbox cloud, so it works with or without Home Assistant. It monitors every charger on your Wallbox account, can register them with Predbat as cars, and can pause and resume them from Predbat's car charging plan.

#### What it publishes (wallbox)

For each charger, with `<id>` being the Wallbox charger id:

| Entity | Description |
| ------ | ----------- |
| `sensor.predbat_wallbox_<id>_status` | Charger status, e.g. `Charging`, `Paused`, `Waiting for car demand`, `Ready` |
| `sensor.predbat_wallbox_<id>_power` | Charging power in W |
| `sensor.predbat_wallbox_<id>_session_energy` | Energy added in the current session in kWh; resets with each session |
| `binary_sensor.predbat_wallbox_<id>_connected` | On while a car is plugged in |
| `binary_sensor.predbat_wallbox_<id>_charging` | On while the car is charging |
| `switch.predbat_wallbox_<id>_charging` | Turn off to pause charging, on to resume a paused session |
| `switch.predbat_wallbox_<id>_locked` | Lock or unlock the charger |
| `number.predbat_wallbox_<id>_max_charging_current` | Maximum charging current in A |
| `select.predbat_wallbox_<id>_eco_smart` | Eco-Smart mode: `off`, `eco_mode` or `full_solar`. Only on chargers with Eco-Smart |

Wallbox chargers cannot report the car's state of charge, so there is no battery sensor.

The controls need a Wallbox account with admin rights over the charger. Without them monitoring still works and Predbat logs a warning the first time a control is refused.

#### Automatic configuration (wallbox)

With `wallbox_automatic` on (the default), Predbat sets **car_charging_energy**, **car_charging_planned**, **car_charging_power** and **car_charging_now** to the Wallbox entities, and raises **num_cars** to the number of chargers. Chargers are taken in order of their id, so the first charger is car 0. A **car_charging_now** you have set yourself is kept.

Predbat does not set **car_charging_soc**. If you want Predbat to plan to a target charge level, set it from your car's own integration.

#### Predbat-led charging (wallbox)

Set `wallbox_control: True` to let Predbat pause and resume each charger from its own car charging plan. It needs `wallbox_automatic`. While it is active Predbat publishes `switch.predbat_wallbox_control`, which you can turn off to hand the chargers back at any time; read only mode does the same.

- Inside a planned car charging slot a paused charger is resumed.
- Outside a slot a charging charger is paused.
- When control is released, any charger Predbat paused is resumed and returned to its own Wallbox schedule.

Things to know:

- Wallbox can only pause a charger once it is charging, so a car plugged in outside a slot draws power until Predbat's next poll (two minutes by default).
- Clear any schedule set in the Wallbox app. A charger that Wallbox is holding in `Scheduled` will not charge in a Predbat slot.
- Predbat never unlocks a locked charger. It logs a warning if a locked charger misses its slot.
- Some cars go to sleep when charging is paused and do not wake when it is resumed. If yours does, Predbat-led charging will not suit it.

#### Configuration Options (wallbox)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `username` | String | Yes | - | `wallbox_username` | Your Wallbox account email address |
| `password` | String | Yes | - | `wallbox_password` | Your Wallbox account password |
| `automatic` | Boolean | No | `True` | `wallbox_automatic` | Register the chargers with Predbat as cars |
| `wallbox_control` | Boolean | No | `False` | `wallbox_control` | Let Predbat pause and resume the chargers from its own plan. Requires `wallbox_automatic`; released by read only mode |
| `poll_seconds` | Integer | No | `120` | `wallbox_poll_seconds` | How often to poll Wallbox, 60 to 1800, in steps of 60 |
````

Also add `wallbox_username` and `wallbox_password` to the list of secret ids near line 278, in the same style as `ohme_login`.

- [ ] **Step 2: `docs/car-charging.md`**

After the Ohme sections and before `## GivEnergy Gateway OCPP EV charger` (about line 423), add:

````markdown
## Wallbox car charger direct integration

Predbat can talk directly to your Wallbox charger by configuring your Wallbox account details in `apps.yaml`:

```yaml
  wallbox_username: !secret wallbox_username
  wallbox_password: !secret wallbox_password
  # Let Predbat pause and resume the charger from its own car charging plan
  #wallbox_control: True
```

Predbat then registers each charger on the account as a car and sets **car_charging_energy**, **car_charging_planned**, **car_charging_power** and **car_charging_now** for you. A Wallbox charger cannot report the car's state of charge, so set **car_charging_soc** from your car's own integration if you want Predbat to plan to a target.

See [Wallbox Charger](components.md#wallbox-charger-wallbox) for the entities it publishes and how Predbat-led charging behaves.

If you would rather use the Home Assistant Wallbox integration, see [Wallbox Pulsar](devices.md#wallbox-pulsar).
````

In each of the four sentences that list the supported charger integrations (about lines 108, 211, 521 and 537), add `Wallbox` after `myenergi Zappi`.

- [ ] **Step 3: `docs/apps-yaml.md`**

After the `ohme_automatic_octopus_intelligent` bullet (about line 2112), add:

```markdown
- **wallbox_username** - Wallbox EV charger account email address
- **wallbox_password** - Password for the above Wallbox account
- **wallbox_automatic** - Register the Wallbox chargers with Predbat as cars (default `True`)
- **wallbox_control** - Let Predbat pause and resume the Wallbox chargers from its own car charging plan (default `False`)
- **wallbox_poll_seconds** - How often to poll the Wallbox cloud in seconds (default `120`)
```

Add `wallbox_username` and `wallbox_password` to the sample secrets near lines 239–240, next to the Ohme ones and in the same form.

- [ ] **Step 4: `docs/devices.md`**

In the `## Wallbox Pulsar` section (about line 269), add as its first paragraph:

```markdown
Predbat can talk to Wallbox directly, with no Home Assistant integration needed: see [Wallbox Charger](components.md#wallbox-charger-wallbox). The configuration below is for the Home Assistant Wallbox integration instead.
```

- [ ] **Step 5: `apps/predbat/config/apps.yaml`**

After the Ohme block (ends about line 320), add:

```yaml
  # Wallbox EV charger cloud direct integration
  #wallbox_username: !secret wallbox_username
  #wallbox_password: !secret wallbox_password
  # Register the Wallbox chargers with Predbat as cars (on by default)
  #wallbox_automatic: True
  # Let Predbat pause and resume the chargers according to its own car charging plan.
  # Requires wallbox_automatic
  #wallbox_control: True
```

- [ ] **Step 6: Component counts**

In `CLAUDE.md` (line 113) and `AGENTS.md` (line 109), add `Wallbox` to the name list after `Ohme` and correct the count in "registry of 18 pluggable components". The 18 is already stale: count the entries with `grep -c '"class":' apps/predbat/components.py` after Task 5 and use that number. In `.github/copilot-instructions.md`, add `WallboxAPI` to the component class list (line 139) and `wallbox.py` to the file list (line 328).

- [ ] **Step 7: Check and commit**

```bash
./run_pre_commit > /tmp/wallbox_precommit.log 2>&1; tail -40 /tmp/wallbox_precommit.log
```

Expected: every hook `Passed`. If CSpell flags a real word (for example `ecosmart`), add it to `.cspell/custom-dictionary-workspace.txt`, run pre-commit again (it re-sorts that file), and stage the result.

```bash
git add docs/components.md docs/car-charging.md docs/apps-yaml.md docs/devices.md apps/predbat/config/apps.yaml CLAUDE.md AGENTS.md .github/copilot-instructions.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(wallbox): document the Wallbox component"
```

---

### Task 10: Full verification and live check (needs the maintainer's charger)

**Files:**
- Modify: `tools/debug-journal.md` (a Wallbox entry)

- [ ] **Step 1: Run the quick suite**

```bash
cd coverage
./run_all --quick > /tmp/wallbox_quick.log 2>&1; tail -40 /tmp/wallbox_quick.log
```

Expected: all tests pass. Tests that enumerate components or config keys (`components`, `validate_config`, `component_base`, `agent_tools`) are the likely ones to react to a new registry entry; if one fails, read its assertion in the log before changing anything.

- [ ] **Step 2: Run pre-commit over everything**

```bash
./run_pre_commit > /tmp/wallbox_precommit.log 2>&1; tail -40 /tmp/wallbox_precommit.log
```

Expected: every hook `Passed`, including `interrogate` at 100%.

- [ ] **Step 3: Live check (maintainer)**

With a car plugged in and charging, from `apps/predbat/`:

```bash
python3 wallbox.py --username <email> --password <password> --pause
python3 wallbox.py --username <email> --password <password> --resume
python3 wallbox.py --username <email> --password <password> --resume-schedule
```

Expected after `--pause`: the read-back shows `status='Paused'`, `paused=True`. After `--resume`: `status='Charging'`. `--resume-schedule` returns without error.

Then run Predbat with `wallbox_username`, `wallbox_password` and `wallbox_control: True` and confirm, in order:

1. The `predbat_wallbox_<id>_*` entities appear with sensible values.
2. The log shows `car_charging_energy`, `car_charging_planned`, `car_charging_power` and `car_charging_now` being set.
3. With the car charging outside a planned car slot, Predbat pauses it within one poll.
4. Turning `switch.predbat_wallbox_control` off resumes the charger.
5. Over an hour of running there is no `rate limited` warning at the default 120 second poll.

- [ ] **Step 4: Record what was learnt**

Add a `Wallbox` entry to `tools/debug-journal.md` in that file's existing style, recording: which `User-Agent` the API accepted; the status ids actually seen for charging, paused and unplugged; whether a 120 second poll stayed clear of the rate limit; how long a pause took to show in the status; and anything in the live check that differed from this plan.

- [ ] **Step 5: Commit**

```bash
git add tools/debug-journal.md
git commit -m "docs(journal): Wallbox API findings from the live check"
```
