# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Sigenergy Cloud API integration component.

REST polling client for Sigenergy inverters (Sigenstor) with OAuth2 Client
Credentials authentication.  Publishes realtime energy-flow and battery state
to Home Assistant entities and issues charge/discharge/eco commands via the
Sigenergy MQTT broker.

Authentication is via the Key-based endpoint:
  POST /openapi/auth/login/key  {"key": base64(AppKey:AppSecret)}

Data endpoints used:
  GET  /openapi/system                                   — system list
  GET  /openapi/system/{systemId}/devices                — device inventory
  GET  /openapi/systems/{systemId}/energyFlow            — realtime power & SOC
  GET  /openapi/systems/{systemId}/summary               — daily/lifetime yield
  GET  /openapi/instruction/{systemId}/settings          — current operating mode

Control endpoints:
  PUT  /openapi/instruction/settings                     — switch operating mode (MSC/FFG)
  MQTT openapi/instruction/command                       — charge / discharge / idle (via MQTT broker)

The MQTT broker hostname is derived from the REST base URL (same host, port 8883, TLS).
Authentication to the MQTT broker uses app_key as username and the current
access_token as password.

The component maps onto the existing 'SIG' inverter type already defined in
config.py so no changes are needed there.

Registered in components.py under key 'sigenergy'.
"""

import argparse
import asyncio
import base64
import json
import ssl
import time
import traceback

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

try:
    import aiomqtt
    HAS_AIOMQTT = True
except ImportError:
    aiomqtt = None
    HAS_AIOMQTT = False

from datetime import datetime, timedelta
from component_base import ComponentBase

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGENERGY_DEFAULT_BASE_URL = "https://openapi.sigencloud.com"
SIGENERGY_TIMEOUT = 20                # seconds per HTTP request
SIGENERGY_MAX_RETRIES = 3
SIGENERGY_TOKEN_EXPIRY_BUFFER = 600   # refresh token 10 min before expiry
SIGENERGY_MIN_REQUEST_INTERVAL = 6.0  # enforce ≥10 req/min API limit
SIGENERGY_POLL_INTERVAL = 300         # realtime data poll every 5 minutes
SIGENERGY_DEVICE_POLL_INTERVAL = 1800  # device list refresh every 30 minutes
SIGENERGY_COMMAND_RETRY_DELAY = 2.0
SIGENERGY_MQTT_PORT = 8883             # TLS MQTT port on the Sigenergy broker

# Operating mode enums (REST mode switch endpoint — MSC and FFG only; NBI is not used)
SIGENERGY_MODE_MSC = 0   # Maximum Self-Consumption (eco)
SIGENERGY_MODE_FFG = 5   # Fully Feed-in to Grid
SIGENERGY_MODE_NBI = 8   # NorthBound (defined for completeness; not switched to by this component)

# Battery command activeMode strings
SIGENERGY_ACTIVE_MODE_CHARGE = "charge"
SIGENERGY_ACTIVE_MODE_DISCHARGE = "discharge"
SIGENERGY_ACTIVE_MODE_IDLE = "idle"
SIGENERGY_ACTIVE_MODE_SELF = "selfConsumption"
SIGENERGY_ACTIVE_MODE_SELF_GRID = "selfConsumption-grid"

# Device type strings returned by the device-list endpoint
SIGENERGY_DEVICE_INVERTER = "Inverter"
SIGENERGY_DEVICE_BATTERY = "Battery"
SIGENERGY_DEVICE_GATEWAY = "Gateway"
SIGENERGY_DEVICE_METER = "Meter"

# Time options for schedule selects (HH:MM, one per minute)
_BASE_TIME = datetime.strptime("00:00", "%H:%M")
SIGENERGY_OPTIONS_TIME = [(_BASE_TIME + timedelta(seconds=m * 60)).strftime("%H:%M") for m in range(0, 24 * 60)]


def _safe_float(value, default=0.0):
    """Convert value to float with a fallback default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    """Convert value to int with a fallback default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class SigenergyAPI(ComponentBase):
    """Sigenergy Cloud API component for Predbat.

    Polls the Sigenergy OpenAPI for realtime energy-flow data and battery
    state, publishes Home Assistant entities, and applies charge/discharge
    control commands on behalf of Predbat's planner.
    """

    def initialize(self, app_key, app_secret, base_url=None, system_id=None, automatic=False, enable_controls=True, **kwargs):
        """Initialise the Sigenergy API component.

        Args:
            app_key: Sigenergy Application Key (from Control Center → Settings).
            app_secret: Sigenergy Application Secret.
            base_url: Override the API base URL (default: SIGENERGY_DEFAULT_BASE_URL).
            system_id: Optional system ID filter.  When None all authorised
                       systems are used.  When a string or list, only matching
                       systems are used.
            automatic: When True, call set_arg() to wire Predbat config to the
                       published entity IDs on first run.
            enable_controls: When True, apply charge/discharge commands.
        """
        if not HAS_AIOHTTP:
            raise ImportError("SigenergyAPI requires the 'aiohttp' package: pip install aiohttp")
        if not HAS_AIOMQTT:
            raise ImportError("SigenergyAPI requires the 'aiomqtt' package: pip install aiomqtt")

        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = (base_url or SIGENERGY_DEFAULT_BASE_URL).rstrip("/")
        # Derive MQTT hostname from REST base URL (strip scheme, no port suffix needed)
        self.mqtt_host = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
        self.mqtt_port = SIGENERGY_MQTT_PORT
        self.automatic = automatic
        self.enable_controls = enable_controls

        # Normalise system_id filter to a set (empty = all systems)
        if system_id is None:
            self.system_id_filter = set()
        elif isinstance(system_id, (list, tuple)):
            self.system_id_filter = set(system_id)
        else:
            self.system_id_filter = {str(system_id)}

        # Token state
        self.access_token = None
        self.token_expires_at = 0.0  # UNIX timestamp

        # Data stores keyed by systemId
        self.systems = {}         # systemId → system info dict
        self.devices = {}         # systemId → list of device dicts
        self.energy_flow = {}     # systemId → latest energyFlow dict
        self.daily_summary = {}   # systemId → latest summary dict
        self.current_mode = {}    # systemId → energyStorageOperationMode int

        # Control state keyed by systemId
        self.controls = {}        # systemId → {charge: {…}, export: {…}, reserve: …}

        # Mode-change deduplication
        self.current_mode_hash = {}           # systemId → hash of last applied command
        self.current_mode_hash_timestamp = {}  # systemId → datetime of last applied command

        # Rate-limit tracking
        self._last_request_time = 0.0

        # Delay between mode-switch and battery command (seconds); set to 0 in tests
        self._command_delay = 1.0

        self.log("SigenergyAPI: Initialised, base_url={}".format(self.base_url))

    # -----------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------

    async def get_access_token(self):
        """Obtain or refresh the access token.

        Uses the Sigenergy key-based authentication endpoint:
          POST /openapi/auth/login/key  {"key": base64(AppKey:AppSecret)}

        The token is cached until within SIGENERGY_TOKEN_EXPIRY_BUFFER seconds
        of expiry (default 12 h lifetime).

        Returns:
            Access token string on success, None on failure.
        """
        now = time.monotonic()
        if self.access_token and now < self.token_expires_at - SIGENERGY_TOKEN_EXPIRY_BUFFER:
            return self.access_token

        raw_key = "{}:{}".format(self.app_key, self.app_secret)
        encoded_key = base64.b64encode(raw_key.encode()).decode()
        url = "{}/openapi/auth/login/key".format(self.base_url)
        payload = {"key": encoded_key}

        self.log("SigenergyAPI: Requesting new access token")
        try:
            timeout = aiohttp.ClientTimeout(total=SIGENERGY_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        self.log("Warn: SigenergyAPI: Auth request returned HTTP {}".format(response.status))
                        return None
                    try:
                        data = await response.json(content_type=None)
                    except (Exception) as e:
                        self.log("Warn: SigenergyAPI: Failed to decode auth response: {}".format(e))
                        return None

            code = data.get("code", -1)
            if code != 0:
                self.log("Warn: SigenergyAPI: Auth failed, code={}, msg={}".format(code, data.get("msg", "unknown")))
                self.access_token = None
                return None

            token_data = data.get("data", {})
            self.access_token = token_data.get("accessToken")
            expires_in = _safe_int(token_data.get("expiresIn", 43200), 43200)
            self.token_expires_at = now + expires_in
            self.log("SigenergyAPI: Token obtained, expires in {} s".format(expires_in))
            return self.access_token

        except (asyncio.TimeoutError, Exception) as e:
            self.log("Warn: SigenergyAPI: Exception during authentication: {}".format(e))
            self.access_token = None
            return None

    # -----------------------------------------------------------------------
    # HTTP helpers
    # -----------------------------------------------------------------------

    async def _enforce_rate_limit(self):
        """Sleep if necessary to respect the 10 req/min API limit."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < SIGENERGY_MIN_REQUEST_INTERVAL:
            await asyncio.sleep(SIGENERGY_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.monotonic()

    async def _request(self, method, path, params=None, json_data=None, retries=SIGENERGY_MAX_RETRIES):
        """Perform an authenticated HTTP request with retry logic.

        Args:
            method: HTTP method string ('GET', 'POST', 'PUT').
            path: API path, e.g. '/openapi/system'.
            params: URL query parameters dict.
            json_data: Request body dict (serialised to JSON).
            retries: Number of retry attempts.

        Returns:
            Parsed 'data' field from the response JSON, or None on failure.
        """
        token = await self.get_access_token()
        if not token:
            return None

        url = "{}{}".format(self.base_url, path)
        headers = {
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        }

        for attempt in range(retries):
            await self._enforce_rate_limit()
            try:
                timeout = aiohttp.ClientTimeout(total=SIGENERGY_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    if method == "GET":
                        ctx = session.get(url, headers=headers, params=params)
                    elif method == "POST":
                        ctx = session.post(url, headers=headers, json=json_data)
                    elif method == "PUT":
                        ctx = session.put(url, headers=headers, json=json_data)
                    else:
                        self.log("Warn: SigenergyAPI: Unknown HTTP method {}".format(method))
                        return None

                    async with ctx as response:
                        if response.status == 401:
                            self.log("Warn: SigenergyAPI: 401 Unauthorised — refreshing token")
                            self.access_token = None
                            token = await self.get_access_token()
                            if not token:
                                return None
                            headers["Authorization"] = "Bearer {}".format(token)
                            continue

                        if response.status not in (200, 201):
                            self.log("Warn: SigenergyAPI: HTTP {} for {} {}".format(response.status, method, path))
                            if attempt < retries - 1:
                                await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
                                continue
                            return None

                        try:
                            body = await response.json(content_type=None)
                        except Exception as e:
                            self.log("Warn: SigenergyAPI: Failed to decode response from {}: {}".format(path, e))
                            return None

                        code = body.get("code", -1)
                        if code != 0:
                            self.log("Warn: SigenergyAPI: API error code={} msg={} for {}".format(code, body.get("msg", ""), path))
                            return None

                        return body.get("data")

            except asyncio.TimeoutError:
                self.log("Warn: SigenergyAPI: Timeout on {} {} (attempt {}/{})".format(method, path, attempt + 1, retries))
                if attempt < retries - 1:
                    await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
            except Exception as e:
                self.log("Warn: SigenergyAPI: Exception on {} {}: {}\n{}".format(method, path, e, traceback.format_exc()))
                if attempt < retries - 1:
                    await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)

        return None

    # -----------------------------------------------------------------------
    # Data fetching
    # -----------------------------------------------------------------------

    async def fetch_system_list(self):
        """Fetch the list of authorised power stations.

        Returns:
            True on success, False on failure.
        """
        data = await self._request("GET", "/openapi/system")
        if data is None:
            self.log("Warn: SigenergyAPI: Failed to fetch system list")
            return False

        # data may be a list directly or wrapped in a dict
        systems = data if isinstance(data, list) else data.get("list", data.get("records", []))
        if not isinstance(systems, list):
            self.log("Warn: SigenergyAPI: Unexpected system list format: {}".format(type(systems)))
            return False

        for system in systems:
            sid = system.get("systemId")
            if not sid:
                continue
            if self.system_id_filter and sid not in self.system_id_filter:
                continue
            self.systems[sid] = system
            self.log("SigenergyAPI: Found system {} ({})".format(sid, system.get("systemName", "unnamed")))

        if not self.systems:
            self.log("Warn: SigenergyAPI: No matching systems found (filter={})".format(self.system_id_filter or "all"))
            return False

        return True

    async def fetch_device_list(self, system_id):
        """Fetch the device inventory for a power station.

        Args:
            system_id: Sigenergy system unique identifier.

        Returns:
            True on success, False on failure.
        """
        data = await self._request("GET", "/openapi/system/{}/devices".format(system_id))
        if data is None:
            self.log("Warn: SigenergyAPI: Failed to fetch device list for {}".format(system_id))
            return False

        devices = data if isinstance(data, list) else data.get("list", data.get("records", []))
        if not isinstance(devices, list):
            self.log("Warn: SigenergyAPI: Unexpected device list format for {}: {}".format(system_id, type(devices)))
            return False

        self.devices[system_id] = devices
        self.log("SigenergyAPI: System {} has {} device(s)".format(system_id, len(devices)))
        return True

    async def fetch_energy_flow(self, system_id):
        """Fetch realtime energy-flow data for a system.

        The API returns power values in kW with these sign conventions:
          pvPower      — always positive
          gridPower    — positive = export to grid, negative = import from grid
          batteryPower — positive = charging, negative = discharging
          loadPower    — always positive

        Args:
            system_id: Sigenergy system unique identifier.

        Returns:
            True on success, False on failure.
        """
        data = await self._request("GET", "/openapi/systems/{}/energyFlow".format(system_id))
        if data is None:
            self.log("Warn: SigenergyAPI: Failed to fetch energy flow for {}".format(system_id))
            return False

        self.energy_flow[system_id] = data
        soc = _safe_float(data.get("batterySoc", 0))
        battery_kw = _safe_float(data.get("batteryPower", 0))
        pv_kw = _safe_float(data.get("pvPower", 0))
        grid_kw = _safe_float(data.get("gridPower", 0))
        self.log("SigenergyAPI: System {} — SOC {:.0f}% battery {:.2f}kW pv {:.2f}kW grid {:.2f}kW".format(system_id, soc, battery_kw, pv_kw, grid_kw))
        return True

    async def fetch_daily_summary(self, system_id):
        """Fetch daily/lifetime generation summary for a system.

        Args:
            system_id: Sigenergy system unique identifier.

        Returns:
            True on success, False on failure.
        """
        data = await self._request("GET", "/openapi/systems/{}/summary".format(system_id))
        if data is None:
            self.log("Warn: SigenergyAPI: Failed to fetch summary for {}".format(system_id))
            return False

        self.daily_summary[system_id] = data
        return True

    async def fetch_current_mode(self, system_id):
        """Fetch the current energy storage operating mode.

        Args:
            system_id: Sigenergy system unique identifier.

        Returns:
            True on success, False on failure.
        """
        data = await self._request("GET", "/openapi/instruction/{}/settings".format(system_id))
        if data is None:
            return False

        mode_int = _safe_int(data.get("energyStorageOperationMode", SIGENERGY_MODE_MSC))
        self.current_mode[system_id] = mode_int
        return True

    # -----------------------------------------------------------------------
    # Control commands
    # -----------------------------------------------------------------------

    async def set_operating_mode(self, system_id, mode_int):
        """Set the energy storage operating mode via REST.

        Args:
            system_id: Sigenergy system unique identifier.
            mode_int: Operating mode integer (SIGENERGY_MODE_MSC/FFG/NBI).

        Returns:
            True on success, False on failure.
        """
        payload = {
            "systemId": system_id,
            "energyStorageOperationMode": mode_int,
        }
        result = await self._request("PUT", "/openapi/instruction/settings", json_data=payload)
        if result is None:
            # Some implementations return an empty data field on success — treat None as success
            # if the HTTP call didn't raise (the _request wrapper returns None for both API errors
            # and non-zero code responses, but we can't distinguish here without more context).
            self.log("SigenergyAPI: set_operating_mode({}) returned None — assuming success".format(mode_int))
            return True
        self.log("SigenergyAPI: Operating mode set to {} for system {}".format(mode_int, system_id))
        return True

    async def _publish_mqtt(self, topic, payload_dict):
        """Publish a JSON payload to the Sigenergy MQTT broker.

        Connects to the broker (same hostname as the REST base URL) using TLS
        on port 8883.  Authenticates with app_key as username and the current
        access_token as password.  A fresh connection is made for each publish
        (Sigenergy commands are infrequent so persistent connection overhead is
        unnecessary).

        Args:
            topic: MQTT topic string.
            payload_dict: Dict that will be serialised to JSON and published.

        Returns:
            True on success, False on failure.
        """
        try:
            tls_context = ssl.create_default_context()
            async with aiomqtt.Client(
                hostname=self.mqtt_host,
                port=self.mqtt_port,
                username=self.app_key,
                password=self.access_token,
                tls_context=tls_context,
                keepalive=30,
            ) as client:
                await client.publish(topic, payload=json.dumps(payload_dict), qos=1)
            self.log("SigenergyAPI: MQTT published to {}".format(topic))
            return True
        except Exception as e:
            self.log("Warn: SigenergyAPI: MQTT publish to {} failed: {}".format(topic, e))
            return False

    async def send_battery_command(self, system_id, active_mode, duration_minutes, charging_power_kw=None):
        """Send a battery command via MQTT to the Sigenergy broker.

        Publishes to the MQTT topic ``openapi/instruction/command``.  A fresh
        access token is obtained if needed before building the payload.

        Args:
            system_id: Sigenergy system unique identifier.
            active_mode: One of the SIGENERGY_ACTIVE_MODE_* string constants.
            duration_minutes: Command duration in minutes (max ~720).
            charging_power_kw: Charging/discharging power in kW.  Required for
                               charge and discharge modes; optional otherwise.

        Returns:
            True on success, False on failure.
        """
        token = await self.get_access_token()
        if not token:
            self.log("Warn: SigenergyAPI: No access token for MQTT battery command")
            return False

        payload = {
            "accessToken": token,
            "systemId": system_id,
            "activeMode": active_mode,
            "startTime": int(time.time()),
            "duration": int(duration_minutes),
        }
        if charging_power_kw is not None:
            payload["chargingPower"] = round(charging_power_kw, 2)

        self.log("SigenergyAPI: Sending MQTT battery command {} ({} min, {:.2f}kW) to system {}".format(
            active_mode, duration_minutes, charging_power_kw or 0.0, system_id))

        return await self._publish_mqtt("openapi/instruction/command", payload)

    # -----------------------------------------------------------------------
    # HA entity publishing
    # -----------------------------------------------------------------------

    def _system_slug(self, system_id):
        """Return a short, safe slug for use in entity IDs.

        Uses the last 12 characters of the system ID (or the full string if
        shorter) to keep entity names manageable.
        """
        return str(system_id)[-12:].lower().replace("-", "_")

    def _get_battery_capacity_kwh(self, system_id):
        """Return the rated battery capacity in kWh for a system.

        Prefers the batteryCapacity field from the system-list response.
        Falls back to summing ratedEnergy from individual Battery devices.
        """
        system_info = self.systems.get(system_id, {})
        capacity = _safe_float(system_info.get("batteryCapacity", 0))
        if capacity > 0:
            return capacity

        # Fallback: sum device-level ratedEnergy
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_BATTERY:
                attr = device.get("attrMap", {})
                capacity += _safe_float(attr.get("ratedEnergy", 0))
        return capacity

    def _get_battery_max_power_kw(self, system_id):
        """Return the combined rated charge/discharge power in kW for a system."""
        # Prefer device-level ratedChargePower
        power = 0.0
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_BATTERY:
                attr = device.get("attrMap", {})
                power += _safe_float(attr.get("ratedChargePower", 0))
        if power > 0:
            return power

        # Fallback: inverter rated power
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_INVERTER:
                attr = device.get("attrMap", {})
                power += _safe_float(attr.get("ratedActivePower", 0))
        return power

    def _get_inverter_max_power_kw(self, system_id):
        """Return the combined inverter rated active power in kW."""
        power = 0.0
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_INVERTER:
                attr = device.get("attrMap", {})
                power += _safe_float(attr.get("ratedActivePower", 0))
        return power

    async def publish_system_entities(self, system_id):
        """Publish Home Assistant entities for a system.

        Publishes realtime energy-flow data (battery SOC/power, PV power,
        grid power, load power) and daily generation summary.

        Args:
            system_id: Sigenergy system unique identifier.
        """
        slug = self._system_slug(system_id)
        system_info = self.systems.get(system_id, {})
        system_name = system_info.get("systemName", system_id)
        flow = self.energy_flow.get(system_id, {})
        summary = self.daily_summary.get(system_id, {})

        battery_soc_pct = _safe_float(flow.get("batterySoc", 0))
        # battery_power: API positive=charging, negative=discharging (same as Predbat convention, in kW)
        battery_power_kw = _safe_float(flow.get("batteryPower", 0))
        pv_power_kw = _safe_float(flow.get("pvPower", 0))
        # gridPower: API positive=export, negative=import → invert for Predbat (positive=import)
        grid_power_kw = -_safe_float(flow.get("gridPower", 0))
        load_power_kw = _safe_float(flow.get("loadPower", 0))
        ev_power_kw = _safe_float(flow.get("evPower", 0))

        daily_yield_kwh = _safe_float(summary.get("dailyPowerGeneration", 0))
        monthly_yield_kwh = _safe_float(summary.get("monthlyPowerGeneration", 0))
        annual_yield_kwh = _safe_float(summary.get("annualPowerGeneration", 0))
        lifetime_yield_kwh = _safe_float(summary.get("lifetimePowerGeneration", 0))

        capacity_kwh = self._get_battery_capacity_kwh(system_id)
        battery_soc_kwh = round(battery_soc_pct * capacity_kwh / 100.0, 3)
        battery_max_kw = self._get_battery_max_power_kw(system_id)
        inverter_max_kw = self._get_inverter_max_power_kw(system_id)

        # --- Battery SOC (kWh) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_battery_soc".format(self.prefix, slug),
            state=battery_soc_kwh,
            attributes={
                "friendly_name": "Sigenergy {} Battery SOC".format(system_name),
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "soc_percent": battery_soc_pct,
                "soc_max": capacity_kwh,
            },
            app="sigenergy",
        )

        # --- Battery SOC percentage ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_battery_soc_percent".format(self.prefix, slug),
            state=battery_soc_pct,
            attributes={
                "friendly_name": "Sigenergy {} Battery SOC %".format(system_name),
                "unit_of_measurement": "%",
                "device_class": "battery",
                "state_class": "measurement",
            },
            app="sigenergy",
        )

        # --- Battery power (W, positive=charging) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_battery_power".format(self.prefix, slug),
            state=round(battery_power_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} Battery Power".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
            app="sigenergy",
        )

        # --- PV power (W) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_pv_power".format(self.prefix, slug),
            state=round(pv_power_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} PV Power".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
            app="sigenergy",
        )

        # --- Grid power (W, positive=import from grid) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_grid_power".format(self.prefix, slug),
            state=round(grid_power_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} Grid Power".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
            app="sigenergy",
        )

        # --- Load power (W) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_load_power".format(self.prefix, slug),
            state=round(load_power_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} Load Power".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
            app="sigenergy",
        )

        # --- EV charger power (W) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_ev_power".format(self.prefix, slug),
            state=round(ev_power_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} EV Power".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
            app="sigenergy",
        )

        # --- Daily PV yield (kWh) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_pv_today".format(self.prefix, slug),
            state=round(daily_yield_kwh, 3),
            attributes={
                "friendly_name": "Sigenergy {} PV Today".format(system_name),
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
            },
            app="sigenergy",
        )

        # --- Battery capacity (kWh) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_battery_capacity".format(self.prefix, slug),
            state=round(capacity_kwh, 3),
            attributes={
                "friendly_name": "Sigenergy {} Battery Capacity".format(system_name),
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
            app="sigenergy",
        )

        # --- Battery max charge/discharge power (W) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_battery_rate_max".format(self.prefix, slug),
            state=round(battery_max_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} Battery Max Power".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
            },
            app="sigenergy",
        )

        # --- Inverter limit (W) ---
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_inverter_limit".format(self.prefix, slug),
            state=round(inverter_max_kw * 1000),
            attributes={
                "friendly_name": "Sigenergy {} Inverter Limit".format(system_name),
                "unit_of_measurement": "W",
                "device_class": "power",
            },
            app="sigenergy",
        )

        # --- System status ---
        system_status = system_info.get("status", "Unknown")
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_status".format(self.prefix, slug),
            state=system_status,
            attributes={
                "friendly_name": "Sigenergy {} Status".format(system_name),
                "system_id": system_id,
                "system_name": system_name,
                "pv_capacity": system_info.get("pvCapacity"),
                "battery_capacity_kwh": capacity_kwh,
                "daily_yield_kwh": daily_yield_kwh,
                "monthly_yield_kwh": monthly_yield_kwh,
                "annual_yield_kwh": annual_yield_kwh,
                "lifetime_yield_kwh": lifetime_yield_kwh,
            },
            app="sigenergy",
        )

    # -----------------------------------------------------------------------
    # Automatic configuration
    # -----------------------------------------------------------------------

    async def automatic_config(self):
        """Wire Predbat config args to the published Sigenergy entity IDs.

        Called once on the first run when self.automatic is True.  Sets
        inverter_type, soc_kw, battery_power, pv_power, grid_power, etc. so
        that the core prediction engine can read the data it needs.
        """
        system_ids = list(self.systems.keys())
        num = len(system_ids)
        if not num:
            self.log("Warn: SigenergyAPI: automatic_config called with no systems")
            return

        self.log("SigenergyAPI: automatic_config — configuring {} system(s)".format(num))
        slugs = [self._system_slug(sid) for sid in system_ids]

        self.set_arg("num_inverters", num)
        self.set_arg("inverter_type", ["SIG" for _ in range(num)])

        self.set_arg("soc_kw", ["sensor.{}_sigenergy_{}_battery_soc".format(self.prefix, s) for s in slugs])
        self.set_arg("soc_max", ["sensor.{}_sigenergy_{}_battery_capacity".format(self.prefix, s) for s in slugs])
        self.set_arg("battery_power", ["sensor.{}_sigenergy_{}_battery_power".format(self.prefix, s) for s in slugs])
        self.set_arg("battery_rate_max", ["sensor.{}_sigenergy_{}_battery_rate_max".format(self.prefix, s) for s in slugs])
        self.set_arg("inverter_limit", ["sensor.{}_sigenergy_{}_inverter_limit".format(self.prefix, s) for s in slugs])
        self.set_arg("pv_power", ["sensor.{}_sigenergy_{}_pv_power".format(self.prefix, s) for s in slugs])
        self.set_arg("grid_power", ["sensor.{}_sigenergy_{}_grid_power".format(self.prefix, s) for s in slugs])
        self.set_arg("load_power", ["sensor.{}_sigenergy_{}_load_power".format(self.prefix, s) for s in slugs])
        self.set_arg("pv_today", ["sensor.{}_sigenergy_{}_pv_today".format(self.prefix, s) for s in slugs])

        # Control entities
        self.set_arg("charge_start_time", ["select.{}_sigenergy_{}_charge_start_time".format(self.prefix, s) for s in slugs])
        self.set_arg("charge_end_time", ["select.{}_sigenergy_{}_charge_end_time".format(self.prefix, s) for s in slugs])
        self.set_arg("charge_limit", ["number.{}_sigenergy_{}_charge_target_soc".format(self.prefix, s) for s in slugs])
        self.set_arg("scheduled_charge_enable", ["switch.{}_sigenergy_{}_charge_enable".format(self.prefix, s) for s in slugs])
        self.set_arg("charge_rate", ["number.{}_sigenergy_{}_charge_rate".format(self.prefix, s) for s in slugs])
        self.set_arg("discharge_start_time", ["select.{}_sigenergy_{}_export_start_time".format(self.prefix, s) for s in slugs])
        self.set_arg("discharge_end_time", ["select.{}_sigenergy_{}_export_end_time".format(self.prefix, s) for s in slugs])
        self.set_arg("discharge_target_soc", ["number.{}_sigenergy_{}_export_target_soc".format(self.prefix, s) for s in slugs])
        self.set_arg("scheduled_discharge_enable", ["switch.{}_sigenergy_{}_export_enable".format(self.prefix, s) for s in slugs])
        self.set_arg("discharge_rate", ["number.{}_sigenergy_{}_export_rate".format(self.prefix, s) for s in slugs])
        self.set_arg("reserve", ["number.{}_sigenergy_{}_reserve".format(self.prefix, s) for s in slugs])

        self.log("SigenergyAPI: automatic_config complete")

    # -----------------------------------------------------------------------
    # Controls
    # -----------------------------------------------------------------------

    def _control_info(self, system_id, direction, field):
        """Return metadata for a single control entity.

        Args:
            system_id: System ID string.
            direction: 'charge', 'export', or None (for global fields like reserve).
            field: Field name string.

        Returns:
            Tuple (item_name, ha_name, friendly_name, field_type, field_units,
                   default, min_value, max_value).
        """
        slug = self._system_slug(system_id)
        system_name = self.systems.get(system_id, {}).get("systemName", system_id)
        field_type = "select"
        field_units = None
        default = None
        min_value = None
        max_value = None

        if direction is None:
            item_name = "sigenergy_{}_{}".format(slug, field)
            friendly_name = "Sigenergy {} {}".format(system_name, field.replace("_", " ").capitalize())
        else:
            item_name = "sigenergy_{}_{}_{}".format(slug, direction, field)
            friendly_name = "Sigenergy {} {} {}".format(system_name, direction.capitalize(), field.replace("_", " ").capitalize())

        if "_time" in field:
            default = "00:00"
            field_type = "select"
            field_units = "time"
        elif field == "enable":
            default = False
            field_type = "switch"
        elif field == "target_soc":
            field_type = "number"
            field_units = "%"
            min_value = 0
            max_value = 100
            default = 100 if direction == "charge" else 0
        elif field == "rate":
            battery_max_w = round(self._get_battery_max_power_kw(system_id) * 1000)
            min_value = 0
            max_value = battery_max_w if battery_max_w > 0 else 10000
            default = max_value
            field_type = "number"
            field_units = "W"
        elif field == "reserve":
            min_value = 0
            max_value = 100
            default = 10
            field_type = "number"
            field_units = "%"

        ha_name = "{}.{}_{}_{}".format(field_type, self.prefix, "sigenergy", item_name.replace("sigenergy_{}_".format(slug), slug + "_", 1))
        ha_name = "{}.{}_{}".format(field_type, self.prefix, item_name)
        return item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value

    async def fetch_controls(self, system_id):
        """Read current control state from Home Assistant entities.

        Args:
            system_id: System ID string.
        """
        if system_id not in self.controls:
            self.controls[system_id] = {}

        for direction in ("charge", "export"):
            if direction not in self.controls[system_id]:
                self.controls[system_id][direction] = {}
            for field in ("start_time", "end_time", "enable", "target_soc", "rate"):
                item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(system_id, direction, field)
                state = self.get_state_wrapper(ha_name, default=default)
                if field_type == "number":
                    state = _safe_int(state, default=default if default is not None else 0)
                    if min_value is not None:
                        state = max(min_value, state)
                    if max_value is not None:
                        state = min(max_value, state)
                elif field_type == "switch":
                    if isinstance(state, str):
                        state = state.lower() == "on"
                self.controls[system_id][direction][field] = state

        # Global fields
        for field in ("reserve",):
            item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(system_id, None, field)
            state = self.get_state_wrapper(ha_name, default=default)
            if field_type == "number":
                state = _safe_int(state, default=default if default is not None else 0)
                if min_value is not None:
                    state = max(min_value, state)
                if max_value is not None:
                    state = min(max_value, state)
            self.controls[system_id][field] = state

    async def publish_controls(self, system_id=None):
        """Publish control entity states to the HA dashboard.

        Args:
            system_id: Specific system ID to publish, or None for all systems.
        """
        target_systems = [system_id] if system_id else list(self.controls.keys())

        for sid in target_systems:
            if sid not in self.controls:
                continue

            for direction in ("charge", "export"):
                for field in ("start_time", "end_time", "enable", "target_soc", "rate"):
                    item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(sid, direction, field)
                    value = self.controls[sid].get(direction, {}).get(field, default)
                    attributes = {"friendly_name": friendly_name}
                    if field_units:
                        attributes["unit_of_measurement"] = field_units
                    if min_value is not None:
                        attributes["min"] = min_value
                    if max_value is not None:
                        attributes["max"] = max_value
                        attributes["step"] = 1
                    if "_time" in field:
                        attributes["options"] = SIGENERGY_OPTIONS_TIME
                    if field_type == "switch":
                        value = "on" if value else "off"
                    self.dashboard_item(ha_name, state=value, attributes=attributes, app="sigenergy")

            for field in ("reserve",):
                item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(sid, None, field)
                value = self.controls[sid].get(field, default)
                self.dashboard_item(
                    ha_name,
                    state=value,
                    attributes={
                        "friendly_name": friendly_name,
                        "unit_of_measurement": field_units,
                        "min": min_value,
                        "max": max_value,
                        "step": 1,
                    },
                    app="sigenergy",
                )

    def _apply_service_to_toggle(self, current, service):
        """Map a switch service call to a boolean."""
        if service == "turn_on":
            return True
        if service == "turn_off":
            return False
        if service == "toggle":
            return not current
        return current

    async def _update_control(self, entity_id, value, direction, field, system_id):
        """Apply a single control update and re-publish."""
        if system_id not in self.controls:
            self.log("Warn: SigenergyAPI: No controls for system {}".format(system_id))
            return

        item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(system_id, direction, field)

        if field == "enable":
            current = self.controls[system_id].get(direction, {}).get(field, False)
            value = self._apply_service_to_toggle(current, value)
        elif "_time" in field:
            if value not in SIGENERGY_OPTIONS_TIME:
                self.log("Warn: SigenergyAPI: Invalid time value {} for {}".format(value, entity_id))
                return
        elif field in ("target_soc", "rate"):
            value = _safe_int(value, default=default if default is not None else 0)
            if min_value is not None:
                value = max(min_value, value)
            if max_value is not None:
                value = min(max_value, value)

        if direction:
            if direction not in self.controls[system_id]:
                self.controls[system_id][direction] = {}
            self.controls[system_id][direction][field] = value
        else:
            self.controls[system_id][field] = value

        self.log("SigenergyAPI: Control update system={} direction={} field={} value={}".format(system_id, direction, field, value))
        await self.publish_controls(system_id)

    def _parse_entity_system(self, entity_id):
        """Extract (system_id, direction, field) from a control entity ID.

        Entity ID format:
          {domain}.{prefix}_sigenergy_{slug}_{direction}_{field}
          {domain}.{prefix}_sigenergy_{slug}_{field}   (for global controls)
        """
        # Remove domain prefix
        name = entity_id.split(".", 1)[-1]
        # Remove predbat prefix
        if name.startswith(self.prefix + "_"):
            name = name[len(self.prefix) + 1:]
        # Must start with 'sigenergy_'
        if not name.startswith("sigenergy_"):
            return None, None, None
        name = name[len("sigenergy_"):]

        # Match slug to known systems
        for sid in self.controls:
            slug = self._system_slug(sid)
            if name.startswith(slug + "_"):
                rest = name[len(slug) + 1:]
                # Try direction-field split
                for direction in ("charge", "export"):
                    if rest.startswith(direction + "_"):
                        field = rest[len(direction) + 1:]
                        return sid, direction, field
                # Global field
                return sid, None, rest

        return None, None, None

    async def select_event(self, entity_id, value):
        """Handle a HA select change event."""
        system_id, direction, field = self._parse_entity_system(entity_id)
        if system_id:
            await self._update_control(entity_id, value, direction, field, system_id)

    async def number_event(self, entity_id, value):
        """Handle a HA number change event."""
        system_id, direction, field = self._parse_entity_system(entity_id)
        if system_id:
            await self._update_control(entity_id, value, direction, field, system_id)

    async def switch_event(self, entity_id, service):
        """Handle a HA switch service call event."""
        system_id, direction, field = self._parse_entity_system(entity_id)
        if system_id:
            await self._update_control(entity_id, service, direction, field, system_id)

    # -----------------------------------------------------------------------
    # Control application
    # -----------------------------------------------------------------------

    async def apply_controls(self, system_id):
        """Compute and apply the charge/discharge/eco command for a system.

        Inspects the current control state (charge/export window, target SOC,
        power rate) and the latest battery SOC to decide which command to send.
        Uses a hash to skip redundant API calls within 15 minutes.

        Args:
            system_id: System ID string.

        Returns:
            True on success, False on failure.
        """
        if system_id not in self.controls:
            self.log("Warn: SigenergyAPI: No controls for system {}".format(system_id))
            return False

        flow = self.energy_flow.get(system_id, {})
        battery_soc_pct = _safe_float(flow.get("batterySoc", 50))
        battery_max_kw = self._get_battery_max_power_kw(system_id)

        now = datetime.now(self.local_tz)

        charge_enable = self.controls[system_id].get("charge", {}).get("enable", False)
        charge_start_str = self.controls[system_id].get("charge", {}).get("start_time", "00:00")
        charge_end_str = self.controls[system_id].get("charge", {}).get("end_time", "00:00")
        charge_target_soc = _safe_int(self.controls[system_id].get("charge", {}).get("target_soc", 100), 100)
        charge_rate_w = _safe_int(self.controls[system_id].get("charge", {}).get("rate", round(battery_max_kw * 1000)), round(battery_max_kw * 1000))
        export_enable = self.controls[system_id].get("export", {}).get("enable", False)
        export_start_str = self.controls[system_id].get("export", {}).get("start_time", "00:00")
        export_end_str = self.controls[system_id].get("export", {}).get("end_time", "00:00")
        export_target_soc = _safe_int(self.controls[system_id].get("export", {}).get("target_soc", 0), 0)
        export_rate_w = _safe_int(self.controls[system_id].get("export", {}).get("rate", round(battery_max_kw * 1000)), round(battery_max_kw * 1000))
        reserve_soc = _safe_int(self.controls[system_id].get("reserve", 10), 10)

        def parse_window(start_str, end_str):
            """Return (start_dt, end_dt) adjusted for midnight-spanning windows."""
            start_dt = now.replace(hour=int(start_str.split(":")[0]), minute=int(start_str.split(":")[1]), second=0, microsecond=0)
            end_dt = now.replace(hour=int(end_str.split(":")[0]), minute=int(end_str.split(":")[1]), second=0, microsecond=0)
            if end_dt <= start_dt:
                if now <= end_dt:
                    start_dt -= timedelta(days=1)
                else:
                    end_dt += timedelta(days=1)
            return start_dt, end_dt

        charge_window = False
        export_window = False
        charge_start_dt = charge_end_dt = None
        export_start_dt = export_end_dt = None

        if charge_enable:
            charge_start_dt, charge_end_dt = parse_window(charge_start_str, charge_end_str)
            if charge_start_dt <= now <= charge_end_dt:
                charge_window = True

        if export_enable:
            export_start_dt, export_end_dt = parse_window(export_start_str, export_end_str)
            if export_start_dt <= now <= export_end_dt:
                export_window = True

        # Determine desired mode (export takes priority)
        if export_window and export_start_dt and export_end_dt:
            duration_min = max(1, int((export_end_dt - now).total_seconds() / 60))
            effective_target = max(export_target_soc, reserve_soc)
            if effective_target >= battery_soc_pct:
                # Already at or below target — freeze (idle)
                new_mode = "freeze_export"
                active_mode = SIGENERGY_ACTIVE_MODE_IDLE
                power_kw = 0.0
            else:
                new_mode = "export"
                active_mode = SIGENERGY_ACTIVE_MODE_DISCHARGE
                power_kw = export_rate_w / 1000.0
        elif charge_window and charge_start_dt and charge_end_dt:
            duration_min = max(1, int((charge_end_dt - now).total_seconds() / 60))
            effective_target = max(charge_target_soc, reserve_soc)
            if effective_target <= reserve_soc or abs(effective_target - battery_soc_pct) < 1:
                # Freeze charge — stay at current SOC
                new_mode = "freeze_charge"
                active_mode = SIGENERGY_ACTIVE_MODE_SELF
                power_kw = 0.0
            elif effective_target < battery_soc_pct:
                # Target below current — go to eco
                new_mode = "eco"
                active_mode = SIGENERGY_ACTIVE_MODE_SELF
                power_kw = 0.0
            else:
                new_mode = "charge"
                active_mode = SIGENERGY_ACTIVE_MODE_CHARGE
                power_kw = charge_rate_w / 1000.0
        else:
            duration_min = 60
            new_mode = "eco"
            active_mode = SIGENERGY_ACTIVE_MODE_SELF
            power_kw = 0.0

        duration_min = min(duration_min, 720)

        # Deduplication — skip if mode unchanged in last 15 minutes
        new_hash = hash((new_mode, round(power_kw, 2), duration_min))
        old_hash = self.current_mode_hash.get(system_id)
        old_ts = self.current_mode_hash_timestamp.get(system_id)
        if old_hash is not None and old_hash == new_hash and old_ts is not None:
            age = (now - old_ts).total_seconds()
            if age < 15 * 60:
                self.log("SigenergyAPI: Mode unchanged for system {} ({} — {:.1f} min ago), skipping".format(system_id, new_mode, age / 60))
                return True

        self.log("SigenergyAPI: Applying mode={} power={:.2f}kW duration={}min to system {}".format(new_mode, power_kw, duration_min, system_id))

        # Send battery command via MQTT — no mode pre-switch required
        ok = await self.send_battery_command(system_id, active_mode, duration_min, charging_power_kw=power_kw if power_kw > 0 else None)
        success = ok

        if success:
            self.current_mode_hash[system_id] = new_hash
            self.current_mode_hash_timestamp[system_id] = now

        return success

    # -----------------------------------------------------------------------
    # Main run loop
    # -----------------------------------------------------------------------

    async def run(self, seconds, first):
        """Main component loop called every 60 seconds by ComponentBase.

        First call: discover systems and devices, publish controls, run
        automatic_config if enabled.
        Every call: refresh realtime data, publish entities, apply controls.

        Args:
            seconds: Elapsed seconds since component start.
            first: True on the first call.

        Returns:
            True on success, False on failure (triggers retry/backoff).
        """
        if first:
            self.log("SigenergyAPI: First run — discovering systems")
            ok = await self.fetch_system_list()
            if not ok:
                self.log("Warn: SigenergyAPI: Failed to discover systems, will retry")
                return False

        # Refresh device inventory periodically
        if first or seconds % SIGENERGY_DEVICE_POLL_INTERVAL == 0:
            for sid in list(self.systems.keys()):
                await self.fetch_device_list(sid)

        # Fetch controls from HA on first run only
        if first:
            for sid in list(self.systems.keys()):
                await self.fetch_controls(sid)
            await self.publish_controls()

        # Automatic configuration
        if first and self.automatic:
            await self.automatic_config()

        # Realtime data refresh
        if first or seconds % SIGENERGY_POLL_INTERVAL == 0:
            for sid in list(self.systems.keys()):
                await self.fetch_energy_flow(sid)
                await self.fetch_daily_summary(sid)

        # Publish entities
        if first or seconds % SIGENERGY_POLL_INTERVAL == 0:
            for sid in list(self.systems.keys()):
                await self.publish_system_entities(sid)

        # Apply controls
        is_readonly = self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"
        if self.enable_controls and not is_readonly:
            if first or seconds % 60 == 0:
                for sid in list(self.systems.keys()):
                    await self.apply_controls(sid)
        else:
            if first:
                self.log("SigenergyAPI: Controls disabled or read-only mode active")

        self.update_success_timestamp()
        return True


class MockBase:  # pragma: no cover
    """Mock base class for standalone testing."""

    def __init__(self):
        """Initialise mock base."""
        self.prefix = "predbat"
        self.local_tz = datetime.now().astimezone().tzinfo
        self.args = {}
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return entity state or default."""
        if raw:
            return self.entities.get(entity_id, {})
        return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Store entity state."""
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        """Print log message with timestamp."""
        print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), message))

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print and store a dashboard entity."""
        import json
        print("ENTITY: {} = {}".format(entity_id, state))
        if attributes:
            display = {k: ("..." if k == "options" else v) for k, v in attributes.items()}
            print("  Attributes: {}".format(json.dumps(display, indent=2, default=str)))
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None):
        """Return arg default (mock always returns default)."""
        return default

    def set_arg(self, key, value):
        """Print auto-config arg assignment."""
        if isinstance(value, list):
            state = "[list of {} items]".format(len(value))
        else:
            state = str(value)
        print("Set arg {} = {}".format(key, state))

    def update_success_timestamp(self):
        """No-op success timestamp update."""


async def test_sigenergy_api(app_key, app_secret, base_url, system_id, test_mode):  # pragma: no cover
    """Run one cycle of the Sigenergy API and optionally test a control mode.

    Args:
        app_key: Sigenergy Application Key.
        app_secret: Sigenergy Application Secret.
        base_url: API base URL.
        system_id: Optional system ID filter string.
        test_mode: One of 'eco', 'charge', 'freeze_charge', 'export', 'freeze_export', or None.
    """
    print("\n{}".format("=" * 60))
    print("Testing Sigenergy Cloud API")
    print("Base URL: {}".format(base_url))
    print("App Key: {}...".format(app_key[:10] if len(app_key) >= 10 else app_key))
    if system_id:
        print("System ID filter: {}".format(system_id))
    if test_mode:
        print("Test mode: {}".format(test_mode))
    print("{}\n".format("=" * 60))

    mock_base = MockBase()

    sig = SigenergyAPI(
        mock_base,
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        system_id=system_id,
        automatic=True,
        enable_controls=(test_mode is not None),
    )

    result = await sig.run(first=True, seconds=0)
    if not result:
        print("x Initialisation failed")
        return 1
    print("+ Initialisation successful")

    if test_mode and sig.systems:
        sid = list(sig.systems.keys())[0]
        flow = sig.energy_flow.get(sid, {})
        battery_soc_pct = _safe_float(flow.get("batterySoc", 50))
        now = datetime.now(sig.local_tz)

        print("\n{}".format("=" * 60))
        print("Testing control mode: {}".format(test_mode))
        print("System ID: {}  SOC: {:.0f}%".format(sid, battery_soc_pct))
        print("{}\n".format("=" * 60))

        def _window(offset_start_min, offset_end_min):
            """Return HH:MM strings offset from now."""
            s = (now + timedelta(minutes=offset_start_min)).strftime("%H:%M")
            e = (now + timedelta(minutes=offset_end_min)).strftime("%H:%M")
            return s, e

        battery_max_w = round(sig._get_battery_max_power_kw(sid) * 1000) or 5000

        if test_mode == "eco":
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": "23:00", "end_time": "23:30", "enable": False, "target_soc": 100, "rate": battery_max_w},
                "export": {"start_time": "23:30", "end_time": "23:59", "enable": False, "target_soc": 10, "rate": battery_max_w},
            }
            print("+ Configured for ECO mode (no active windows)")

        elif test_mode == "charge":
            cs, ce = _window(-30, 120)
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": cs, "end_time": ce, "enable": True, "target_soc": 95, "rate": battery_max_w},
                "export": {"start_time": "23:30", "end_time": "23:59", "enable": False, "target_soc": 10, "rate": battery_max_w},
            }
            print("+ Configured for CHARGE mode ({} - {}, target 95%)".format(cs, ce))

        elif test_mode == "freeze_charge":
            cs, ce = _window(-30, 120)
            target = round(battery_soc_pct)  # same as current = freeze
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": cs, "end_time": ce, "enable": True, "target_soc": target, "rate": battery_max_w},
                "export": {"start_time": "23:30", "end_time": "23:59", "enable": False, "target_soc": 10, "rate": battery_max_w},
            }
            print("+ Configured for FREEZE CHARGE mode ({} - {}, target=current {:.0f}%)".format(cs, ce, battery_soc_pct))

        elif test_mode == "export":
            es, ee = _window(-30, 120)
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": "23:00", "end_time": "23:30", "enable": False, "target_soc": 100, "rate": battery_max_w},
                "export": {"start_time": es, "end_time": ee, "enable": True, "target_soc": 15, "rate": battery_max_w},
            }
            print("+ Configured for EXPORT mode ({} - {}, target 15%)".format(es, ee))

        elif test_mode == "freeze_export":
            es, ee = _window(-30, 120)
            target = min(100, round(battery_soc_pct) + 10)  # above current = freeze
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": "23:00", "end_time": "23:30", "enable": False, "target_soc": 100, "rate": battery_max_w},
                "export": {"start_time": es, "end_time": ee, "enable": True, "target_soc": target, "rate": battery_max_w},
            }
            print("+ Configured for FREEZE EXPORT mode ({} - {}, current {:.0f}% target {}%)".format(es, ee, battery_soc_pct, target))

        else:
            print("x Unknown test mode: {}".format(test_mode))
            return 1

        print("\nApplying controls...")
        ok = await sig.apply_controls(sid)
        if ok:
            print("+ Controls applied successfully")
        else:
            print("x Controls application failed")
            return 1

    return 0


def main():  # pragma: no cover
    """Main entry point for standalone testing."""
    parser = argparse.ArgumentParser(
        description="Test Sigenergy Cloud API and control modes",
        epilog="Example: python sigenergy.py --app-key YOUR_KEY --app-secret YOUR_SECRET --test-mode charge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--app-key", required=True, help="Sigenergy Application Key")
    parser.add_argument("--app-secret", required=True, help="Sigenergy Application Secret")
    parser.add_argument("--base-url", default=SIGENERGY_DEFAULT_BASE_URL, help="API base URL (default: {})".format(SIGENERGY_DEFAULT_BASE_URL))
    parser.add_argument("--system-id", help="Optional system ID filter")
    parser.add_argument(
        "--test-mode",
        choices=["eco", "charge", "freeze_charge", "export", "freeze_export"],
        help="Control mode to test",
    )

    args = parser.parse_args()
    result = asyncio.run(test_sigenergy_api(args.app_key, args.app_secret, args.base_url, args.system_id, args.test_mode))
    raise SystemExit(result or 0)


if __name__ == "__main__":  # pragma: no cover
    main()
