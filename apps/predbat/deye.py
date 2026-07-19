# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# DEYE Cloud API Library
# -----------------------------------------------------------------------------

"""DEYE Cloud API integration for Predbat.

Registers each DEYE battery inverter as a ``DeyeCloud`` Predbat inverter,
publishing monitoring sensors and Fox-style schedule control entities. Predbat
drives those entities through the generic Inverter class; this module derives
the DEYE work mode internally and applies a combined ``strategy_dynamic_control``
payload. Supports HA add-on (self-managed token) and Predbat.com SaaS (injected
token) auth.
"""

import hashlib
import asyncio
import aiohttp
from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from deye_const import DEYE_BASE_URLS, DEYE_ENDPOINTS, DEYE_TIMEOUT, DEYE_RETRIES


class DeyeAPI(ComponentBase, OAuthMixin):
    """DEYE Cloud API component."""

    def initialize(self, app_id="", app_secret="", username="", password="", data_center="eu", company_id="", auth_method="app_credentials", token_expires_at=None, token_hash="", inverter_sn=None, automatic=False, automatic_ignore_pv=False, **kwargs):
        """Initialise the DEYE component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components
        registry has already resolved each arg from its deye_* config key and
        passes it BY ARG NAME (e.g. data_center <- deye_data_center), exactly
        like fox/enphase/solax/teslemetry. Consume the kwargs directly — do NOT
        re-derive with get_arg("data_center"): that bare name is not in
        apps.yaml (the key is deye_data_center), so it would always return the
        default and silently pin every setting.
        """
        self.log("Info: DeyeAPI initialising")
        self.app_id = app_id
        self.app_secret = app_secret
        self.username = username
        self.password = password
        self.data_center = data_center or "eu"
        self.company_id = company_id
        self.token_hash = token_hash
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.applied_payload = {}
        self.cached_values = {}
        self._init_oauth(
            auth_method=auth_method,
            key=app_secret or token_hash,
            token_expires_at=token_expires_at,
            provider_name="deye",
        )

    @property
    def base_url(self):
        """Return the OpenAPI base URL for the configured data centre."""
        return DEYE_BASE_URLS.get(self.data_center, DEYE_BASE_URLS["eu"])

    @staticmethod
    def _sha256(password):
        """Return the lower-case hex SHA-256 of a password."""
        return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()

    @staticmethod
    def _login_payload(login):
        """Choose the DEYE login key: email if it looks like one, else username."""
        login = (login or "").strip()
        return {"email": login} if "@" in login else {"username": login}

    def _auth_headers(self):
        """Return JSON + Bearer auth headers for a DEYE request."""
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.access_token}"}

    async def fetch_token(self):
        """Fetch an access token using app credentials (app_credentials mode)."""
        url = f"{self.base_url}{DEYE_ENDPOINTS['token']}?appId={self.app_id}"
        body = {"appSecret": self.app_secret, "password": self._sha256(self.password), **self._login_payload(self.username)}
        if self.company_id:
            body["companyId"] = str(self.company_id)
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: DEYE token fetch failed: {e}")
            return False
        if not data.get("success"):
            self.log(f"Warn: DEYE token rejected: {data.get('msg', 'unknown')}")
            return False
        self.access_token = data.get("accessToken")
        return True

    async def _post(self, endpoint_key, body):
        """POST to a DEYE endpoint with retry and 401-refresh. Returns parsed JSON or raises."""
        url = f"{self.base_url}{DEYE_ENDPOINTS[endpoint_key]}"
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        last_err = None
        for attempt in range(DEYE_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._auth_headers(), json=body) as resp:
                        if resp.status in (401, 403):
                            self.log(f"Warn: DEYE 401/403 on {endpoint_key}, attempt {attempt + 1}")
                            if await self.handle_oauth_401():
                                continue
                            raise RuntimeError(f"DEYE auth failed on {endpoint_key}")
                        resp.raise_for_status()
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                self.log(f"Warn: DEYE network error on {endpoint_key} attempt {attempt + 1}: {e}")
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"DEYE POST failed after {DEYE_RETRIES} retries on {endpoint_key}: {last_err}")

    async def get_station_ids(self):
        """Return station ids visible to the account."""
        data = await self._post("station_list", {})
        if not data.get("success", True):
            self.log(f"Warn: DEYE station/list failed: {data.get('msg', 'unknown')}")
            return []
        stations = data.get("stationList") or []
        return [s.get("id") or s.get("stationId") for s in stations if s.get("id") or s.get("stationId")]

    async def get_device_list(self):
        """Discover battery inverter serials across the account's stations."""
        station_ids = await self.get_station_ids()
        if not station_ids:
            self.log("Warn: DEYE no stations found")
            self.device_list = []
            return []
        devices = []
        page, size = 1, 100
        while True:
            data = await self._post("station_device", {"page": page, "size": size, "stationIds": station_ids})
            if not data.get("success", True):
                self.log(f"Warn: DEYE station/device failed: {data.get('msg', 'unknown')}")
                break
            items = data.get("deviceListItems") or []
            devices.extend(items)
            total = data.get("total")
            if (total is not None and len(devices) >= int(total)) or len(items) < size:
                break
            page += 1
        serials = [x["deviceSn"] for x in devices if x.get("deviceType") == "INVERTER" and x.get("deviceSn")]
        if self.inverter_sn_filter:
            wanted = {s.lower() for s in self.inverter_sn_filter}
            serials = [s for s in serials if s.lower() in wanted]
        self.device_list = serials
        return serials
