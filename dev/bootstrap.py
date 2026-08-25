"""One-shot Home Assistant provisioning for the Predbat dev container.

Talks to a fresh, un-onboarded Home Assistant instance and, with no human in the
loop, completes onboarding and mints a long-lived access token for Predbat to use.
This drives the same private `/api/onboarding` + `/auth/token` + websocket
`auth/long_lived_access_token` sequence the HA frontend itself uses during the
first-run wizard - it is not an officially documented API, so if a future HA
release changes it, this is the file to fix.

Idempotent: if the instance is already onboarded (e.g. a restart re-using the
bind-mounted dev/ha_config volume), it skips straight to minting a fresh token.
"""

import asyncio
import json
import os
import sys
import time

import aiohttp

HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123")
DEV_USERNAME = os.environ.get("HA_DEV_USERNAME", "dev")
DEV_PASSWORD = os.environ.get("HA_DEV_PASSWORD", "devdevdev1")
DEV_NAME = os.environ.get("HA_DEV_NAME", "Predbat Dev")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/shared/ha_token.json")
CLIENT_ID = HA_URL.rstrip("/") + "/"
POLL_INTERVAL = 2
POLL_TIMEOUT = 180


async def wait_for_ha(session):
    """Poll HA until it answers, up to POLL_TIMEOUT seconds.

    Returns the onboarding steps list if onboarding is still in progress (HTTP 200),
    or None once onboarding is complete (HA unloads the onboarding views, so this
    starts 404ing - any other non-200/404 status just keeps us polling/retrying).
    """
    print("Waiting for Home Assistant at {}...".format(HA_URL), flush=True)
    for _ in range(POLL_TIMEOUT // POLL_INTERVAL):
        try:
            async with session.get(HA_URL + "/api/onboarding", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 404:
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(POLL_INTERVAL)
    raise RuntimeError("Home Assistant did not become reachable at {} within {}s".format(HA_URL, POLL_TIMEOUT))


async def onboard(session):
    """Run the owner-creation + core_config/integration onboarding steps. Returns a short-lived access token."""
    print("Creating owner user...", flush=True)
    async with session.post(
        HA_URL + "/api/onboarding/users",
        json={
            "client_id": CLIENT_ID,
            "name": DEV_NAME,
            "username": DEV_USERNAME,
            "password": DEV_PASSWORD,
            "language": "en",
        },
    ) as resp:
        resp.raise_for_status()
        auth_code = (await resp.json())["auth_code"]

    print("Exchanging auth code for an access token...", flush=True)
    async with session.post(
        HA_URL + "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        },
    ) as resp:
        resp.raise_for_status()
        access_token = (await resp.json())["access_token"]

    headers = {"Authorization": "Bearer " + access_token}

    print("Completing core_config/analytics/integration onboarding steps...", flush=True)
    for step, payload in (
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
    ):
        async with session.post(HA_URL + "/api/onboarding/" + step, json=payload, headers=headers) as resp:
            if resp.status not in (200, 400):
                # 400 here usually means "already done" on a re-run - anything else is worth seeing.
                print("Warn: onboarding step {} returned {}: {}".format(step, resp.status, await resp.text()), flush=True)

    return access_token


async def login(session):
    """Instance is already onboarded - log in with the dev credentials instead."""
    print("Already onboarded, logging in as {}...".format(DEV_USERNAME), flush=True)
    async with session.post(
        HA_URL + "/auth/login_flow",
        json={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID},
    ) as resp:
        resp.raise_for_status()
        flow = await resp.json()

    async with session.post(
        HA_URL + "/auth/login_flow/" + flow["flow_id"],
        json={"username": DEV_USERNAME, "password": DEV_PASSWORD, "client_id": CLIENT_ID},
    ) as resp:
        resp.raise_for_status()
        result = await resp.json()
        auth_code = result["result"]

    async with session.post(
        HA_URL + "/auth/token",
        data={"grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT_ID},
    ) as resp:
        resp.raise_for_status()
        return (await resp.json())["access_token"]


async def mint_long_lived_token(session, access_token):
    """Long-lived tokens are only mintable over the websocket API, not REST."""
    print("Minting a long-lived access token over the websocket API...", flush=True)
    ws_url = HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    async with session.ws_connect(ws_url) as ws:
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": access_token})
        auth_result = await ws.receive_json()
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError("Websocket auth failed: {}".format(auth_result))

        # client_name must be unique per token on a re-run (e.g. after a restart that
        # kept the HA volume but not dev/shared) - HA errors minting a second token
        # under a name that's already in use, so timestamp it.
        await ws.send_json(
            {
                "id": 1,
                "type": "auth/long_lived_access_token",
                "client_name": "predbat-dev-container-{}".format(int(time.time())),
                "lifespan": 3650,
            }
        )
        result = await ws.receive_json()
        if not result.get("success"):
            raise RuntimeError("Failed to mint long-lived access token: {}".format(result))
        return result["result"]


async def main():
    async with aiohttp.ClientSession() as session:
        onboarding_status = await wait_for_ha(session)

        already_onboarded = onboarding_status is None or all(step.get("done") for step in onboarding_status)
        access_token = await login(session) if already_onboarded else await onboard(session)

        ha_key = await mint_long_lived_token(session, access_token)

    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({"ha_url": HA_URL, "ha_key": ha_key}, f)
    print("Wrote {}".format(TOKEN_FILE), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("Error: dev HA bootstrap failed: {}".format(e), file=sys.stderr, flush=True)
        sys.exit(1)
