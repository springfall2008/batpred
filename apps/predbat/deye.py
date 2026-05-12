# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""DEYE Cloud API integration.

Cloud API client for DEYE inverters via the DeyeCloud OpenAPI (EMEA data centre).
Supports real-time monitoring and Time-of-Use charge/discharge schedule control.
The bearer token is injected by the PredBat SaaS platform and refreshed via
the oauth-refresh edge function; this module never calls the token endpoint itself.
"""

import aiohttp
import asyncio
from datetime import datetime
from component_base import ComponentBase
from oauth_mixin import OAuthMixin

DEYE_BASE_URL = "https://eu1-developer.deyecloud.com"
DEYE_TIMEOUT = 30
DEYE_RETRIES = 3


class DeyeAPI(ComponentBase, OAuthMixin):
    """DEYE Cloud API component for PredBat SaaS."""

    def initialise(self):
        """Initialise the DEYE API component."""
        self.log("Info: DeyeAPI initialising")
        self.device_sn = self.get_arg("device_sn", "")
        if isinstance(self.device_sn, list):
            self.device_sn = self.device_sn[0] if self.device_sn else ""

        key = self.get_arg("key", "")
        token_expires_at = self.get_arg("token_expires_at", None)
        token_hash = self.get_arg("token_hash", "")
        self._init_oauth(
            auth_method="oauth",
            key=key,
            token_expires_at=token_expires_at,
            provider_name="deye",
        )
        self.token_hash = token_hash
        self.cached_values = {}

    def _auth_headers(self):
        """Build Authorization header for a DEYE API request."""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

    async def _post(self, path: str, body: dict) -> dict:
        """POST to DEYE API, retrying on transient errors. Returns parsed JSON or raises."""
        url = f"{DEYE_BASE_URL}{path}"
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        last_err = None

        for attempt in range(DEYE_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._auth_headers(), json=body) as resp:
                        if resp.status in (401, 403):
                            self.log(f"Warn: DEYE API 401/403 on {path}, attempt {attempt + 1}")
                            if await self.handle_oauth_401():
                                continue  # retry with refreshed token
                            raise RuntimeError(f"DEYE OAuth auth failed on {path}")
                        resp.raise_for_status()
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                self.log(f"Warn: DEYE API network error on {path} attempt {attempt + 1}: {e}")
                await asyncio.sleep(2**attempt)

        raise RuntimeError(f"DEYE API failed after {DEYE_RETRIES} retries on {path}: {last_err}")

    async def fetch_device_data(self) -> dict:
        """Fetch latest real-time data for the configured device."""
        data = await self._post("/v1.0/device/latest", {"deviceSnList": [self.device_sn]})
        if not data.get("success"):
            raise RuntimeError(f"DEYE device/latest failed: {data.get('msg', 'unknown')}")

        raw_list = data.get("data") or []
        if not raw_list:
            return {}

        device = raw_list[0]
        result = {
            "soc": float(device.get("batteryPower", {}).get("soc", 0)),
            "battery_power": float(device.get("batteryPower", {}).get("power", 0)),
            "grid_power": float(device.get("gridPower", {}).get("power", 0)),
            "pv_power": float(device.get("pvPower", {}).get("power", 0)),
            "load_power": float(device.get("loadOrEpsPower", {}).get("power", 0)),
        }
        self.cached_values[self.device_sn] = result
        return result

    async def set_tou_schedule(self, slots: list) -> bool:
        """Write a Time-of-Use schedule to the inverter."""
        if not slots:
            return False

        payload = {
            "deviceSn": self.device_sn,
            "timeUseSettingItems": slots,
            "timeoutSeconds": 30,
        }
        resp = await self._post("/v1.0/order/sys/tou/update", payload)
        if not resp.get("success"):
            self.log(f"Warn: DEYE TOU update failed: {resp.get('msg', 'unknown')}")
            return False

        order_id = resp.get("data", {}).get("orderId")
        if order_id:
            self.log(f"Info: DEYE TOU update submitted, orderId={order_id}")
        return True

    async def run(self, seconds: int = 0, first: bool = False) -> None:
        """Main component loop — fetch data and apply charge schedule."""
        if not await self.check_and_refresh_oauth_token():
            self.log("Warn: DEYE OAuth token invalid, skipping run")
            return

        if not self.device_sn:
            self.log("Warn: DEYE device_sn not configured, skipping run")
            return

        try:
            device_data = await self.fetch_device_data()
            self.log(f"Info: DEYE SoC={device_data.get('soc', '?')}% " f"battery={device_data.get('battery_power', '?')}W " f"grid={device_data.get('grid_power', '?')}W " f"pv={device_data.get('pv_power', '?')}W")
        except Exception as e:
            self.log(f"Warn: DEYE data fetch failed: {e}")

    async def charge(self, charge_start: datetime, charge_end: datetime, power_w: int, soc_target: int) -> None:
        """Schedule a grid charge window on the inverter."""
        slots = [
            {
                "time": charge_start.strftime("%H:%M"),
                "soc": soc_target,
                "power": power_w,
                "enableGridCharge": True,
                "enableGeneration": True,
            },
            {
                "time": charge_end.strftime("%H:%M"),
                "soc": 10,
                "power": power_w,
                "enableGridCharge": False,
                "enableGeneration": True,
            },
        ]
        await self.set_tou_schedule(slots)

    async def discharge(self, discharge_start: datetime, discharge_end: datetime, power_w: int) -> None:
        """Schedule a forced discharge window on the inverter."""
        slots = [
            {
                "time": discharge_start.strftime("%H:%M"),
                "soc": 10,
                "power": power_w,
                "enableGridCharge": False,
                "enableGeneration": True,
            },
            {
                "time": discharge_end.strftime("%H:%M"),
                "soc": 10,
                "power": power_w,
                "enableGridCharge": False,
                "enableGeneration": True,
            },
        ]
        await self.set_tou_schedule(slots)

    async def final(self) -> None:
        """Cleanup on shutdown."""
        self.log("Info: DeyeAPI shutdown")
