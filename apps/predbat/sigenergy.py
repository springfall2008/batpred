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


Example apps.yaml config:

  sigenergy_system_id: 'XRTKQ1773829273'
  sigenergy_app_key: !secret sigenergy_app_key
  sigenergy_app_secret: !secret sigenergy_app_secret
  sigenergy_client_pem: !secret sigenergy_client_pem
  sigenergy_client_key: !secret sigenergy_client_key
  sigenergy_ca_pem: !secret sigenergy_ca_pem
  sigenergy_automatic: True


TODO:
- Need a way for the user to toggle onboard vs offboard as when onboard they can't control in the app.
- Need to check why test system reports as 24kw max battery rate but 12kw inverter limit, is this real?

"""

import argparse
import asyncio
import base64
import json
import ssl
import tempfile
import time
import traceback
import os

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
from predbat_metrics import record_api_call


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGENERGY_DEFAULT_BASE_URL = "https://openapi-eu.sigencloud.com"  # cspell:disable-line
SIGENERGY_DEFAULT_MQTT_HOST = "mqtt-eu.sigencloud.com"  # cspell:disable-line
SIGENERGY_TIMEOUT = 20                # seconds per HTTP request
SIGENERGY_MAX_RETRIES = 5  # must be >= len(SIGENERGY_RATE_LIMIT_BACKOFF) for full backoff coverage
SIGENERGY_COMMAND_RETRY_DELAY = 2.0

# Sigenergy API response codes
SIGENERGY_CODE_SUCCESS = 0
SIGENERGY_CODE_PARAM_ILLEGAL = 1000
SIGENERGY_CODE_WRONG_SERIAL = 1101
SIGENERGY_CODE_REGISTRATION_INCOMPLETE = 1102
SIGENERGY_CODE_IN_OTHER_VPP = 1103
SIGENERGY_CODE_DEVICE_OFFLINE = 1104
SIGENERGY_CODE_SOFTWARE_NO_VPP = 1105
SIGENERGY_CODE_STATION_NOT_FOUND = 1106
SIGENERGY_CODE_AIO_INVERTER_ONLY = 1107
SIGENERGY_CODE_STATION_INFO_NOT_FOUND = 1108
SIGENERGY_CODE_RPC_FAIL = 1109
SIGENERGY_CODE_INTERFACE_CURRENT_LIMITED = 1110
SIGENERGY_CODE_STATION_NOT_PERMITTED = 1111
SIGENERGY_CODE_IN_OTHER_VPP_EVERGEN = 1112
SIGENERGY_CODE_SYSTEM_PENDING_REVIEW = 1116 # Guess, seems to give this until user approves onboarding
SIGENERGY_CODE_ACCESS_RESTRICTION = 1201
SIGENERGY_CODE_CLIENT_NOT_FOUND = 1301
SIGENERGY_CODE_STATION_STATUS_ANOMALY = 1302
SIGENERGY_CODE_CLIENT_EXISTS = 1303
SIGENERGY_CODE_FIRMWARE_MISMATCH = 1304
SIGENERGY_CODE_NO_PERMISSION_STATION = 1401
SIGENERGY_CODE_NO_PERMISSION = 1402
SIGENERGY_CODE_COMMAND_FAILED = 1501
SIGENERGY_CODE_INTERNAL_ERROR = 1502
SIGENERGY_CODE_ANTI_BACKFLOW_ENABLED = 1503
SIGENERGY_CODE_PEAK_SHAVING_ENABLED = 1504
SIGENERGY_CODE_INVITATION_INVALID = 1600
SIGENERGY_CODE_ACCOUNT_SYSTEM_ERROR = 1601
SIGENERGY_CODE_ACCOUNT_ALREADY_REGISTERED = 1602
SIGENERGY_CODE_ACCOUNT_UNREVIEWED = 1603
SIGENERGY_CODE_DEVELOPER_NOT_APPROVED = 1604
SIGENERGY_TOKEN_EXPIRY_BUFFER = 600   # refresh token 10 min before expiry
SIGENERGY_MIN_REQUEST_INTERVAL = 6.0  # enforce ≥10 req/min API limit
SIGENERGY_POLL_INTERVAL = 300         # realtime data poll every 5 minutes
SIGENERGY_DEVICE_POLL_INTERVAL = 1800  # device list refresh every 30 minutes
SIGENERGY_RATE_LIMIT_BACKOFF = [15, 30, 60, 120, 480]  # seconds to wait after code 1201
SIGENERGY_BATTERY_NOMINAL_VOLTAGE_V = 28.8  # 8S LiFePO4 pack: 8 × 3.6V; used to convert ratedEnergy (Ah) → kWh
SIGENERGY_MQTT_PORT = 8883             # TLS MQTT port on the Sigenergy broker

# MQTT topic patterns — format with app_key and system_id
SIGENERGY_MQTT_TOPIC_CHANGE = "openapi/change/{app_key}/{system_id}"    # system data (device state)
SIGENERGY_MQTT_TOPIC_PERIOD = "openapi/period/{app_key}/{system_id}"    # telemetry data
SIGENERGY_MQTT_TOPIC_ALARM = "openapi/alarm/{app_key}/{system_id}"      # alarm data
SIGENERGY_MQTT_TOPIC_COMMAND = "openapi/instruction/command"            # battery command publish
SIGENERGY_MQTT_TOPIC_MODE = "openapi/instruction/mode"                  # V1 operating mode switch (MQTT)

# Operating mode enums (REST mode switch endpoint — MSC and FFG only; NBI is not used)
SIGENERGY_MODE_MSC = 0   # Maximum Self-Consumption (eco)
SIGENERGY_MODE_FFG = 5   # Fully Feed-in to Grid
SIGENERGY_MODE_VPP = 6   # VPP mode
SIGENERGY_MODE_NBI = 8   # NorthBound (defined for completeness; not switched to by this component)

# Human-readable names for operationalMode integer values
SIGENERGY_MODE_NAMES = {
    0: "Maximum Self-Consumption",
    5: "Fully Feed-in to Grid",
    6: "VPP",
    8: "Northbound Integration",
}

# API mode name strings for the V1 MQTT switch-mode endpoint (openapi/instruction/mode).
# The 'mode' field takes these string values from the Operational Mode Enum.
SIGENERGY_MODE_API_NAMES = {
    SIGENERGY_MODE_MSC: "MSC",
    SIGENERGY_MODE_FFG: "FFG",
    SIGENERGY_MODE_VPP: "VPP",
    SIGENERGY_MODE_NBI: "NBI",
}

# Human-readable names for systemStatus integer values
SIGENERGY_SYSTEM_STATUS_NAMES = {
    0: "Off",
    1: "Online",
    2: "Standby",
    3: "Fault",
}

# Battery command activeMode strings
SIGENERGY_ACTIVE_MODE_CHARGE = "charge"
SIGENERGY_ACTIVE_MODE_DISCHARGE = "discharge"
SIGENERGY_ACTIVE_MODE_IDLE = "idle"
SIGENERGY_ACTIVE_MODE_SELF = "selfConsumption"
SIGENERGY_ACTIVE_MODE_SELF_GRID = "selfConsumption-grid"

# Device type strings returned by the device-list endpoint
SIGENERGY_DEVICE_INVERTER = "Inverter"
SIGENERGY_DEVICE_AIO = "AIO"
SIGENERGY_DEVICE_BATTERY = "Battery"
SIGENERGY_DEVICE_GATEWAY = "Gateway"
SIGENERGY_DEVICE_METER = "Meter"

# Time options for schedule selects (HH:MM:SS, one per minute)
_BASE_TIME = datetime.strptime("00:00:00", "%H:%M:%S")
SIGENERGY_OPTIONS_TIME = [(_BASE_TIME + timedelta(seconds=m * 60)).strftime("%H:%M:%S") for m in range(0, 24 * 60)]

# Sentinel returned by _request() when the API responds with code=0 but an empty/null data field.
# Distinguishes "success with no payload" from None which always means "request failed".
_SIGENERGY_OK = object()


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

    def initialize(self, app_key, app_secret, base_url=None, mqtt_host=None, ca_cert=None, client_cert=None, client_key=None, system_id=None, automatic=False, enable_controls=True, **kwargs):
        """Initialise the Sigenergy API component.

        Args:
            app_key: Sigenergy Application Key (from Control Center → Settings).
            app_secret: Sigenergy Application Secret.
            base_url: Override the API base URL (default: SIGENERGY_DEFAULT_BASE_URL).
            mqtt_host: Override the MQTT broker hostname (default: derived from SIGENERGY_DEFAULT_MQTT_HOST
                       by replacing the regional prefix to match *base_url*).
            ca_cert: PEM text of the CA certificate for verifying the broker's TLS certificate.
            client_cert: PEM text of the client certificate for mutual TLS authentication.
            client_key: PEM text of the client private key for mutual TLS authentication.
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
        # MQTT broker is on a dedicated host separate from the REST endpoint.
        # Derive it from the REST hostname by replacing the 'openapi-' prefix with 'mqtt-'
        # (e.g. openapi-eu.sigencloud.com → mqtt-eu.sigencloud.com) unless overridden.
        if mqtt_host:
            self.mqtt_host = mqtt_host
        else:
            rest_host = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
            self.mqtt_host = rest_host.replace("openapi-", "mqtt-", 1) if rest_host.startswith("openapi-") else SIGENERGY_DEFAULT_MQTT_HOST
        self.mqtt_port = SIGENERGY_MQTT_PORT
        self.ca_cert = ca_cert or self.get_arg("sigenergy_ca_pem", None)
        self.client_cert = client_cert or self.get_arg("sigenergy_client_pem", None)
        self.client_key = client_key or self.get_arg("sigenergy_client_key", None)
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
        self.system_status = {}   # systemId → latest systemStatus dict
        self.daily_summary = {}   # systemId → latest summary dict
        self.current_mode = {}    # systemId → energyStorageOperationMode int

        # Control state keyed by systemId
        self.controls = {}        # systemId → {charge: {…}, export: {…}, reserve: …}

        # Battery command de-duplication — keyed by systemId → (command_hash, startTime)
        self._last_battery_command = {}

        # Last non-zero API response code — set by _request for callers to inspect
        self._last_api_code = 0

        # Rate-limit tracking
        self._last_request_time = 0.0

        # Delay between mode-switch and battery command (seconds); set to 0 in tests
        self._command_delay = 1.0

        # Background MQTT listener task
        self._mqtt_task = None
        self.last_mqtt_update = {}  # UNIX timestamp of last MQTT message received, keyed by system_id
        self._tls_context = None  # cached SSLContext built once on first use

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

        Transient network errors (timeout, connection error, bad HTTP status)
        are retried up to SIGENERGY_MAX_RETRIES times.  API-level rejections
        (wrong credentials, code != 0) are not retried as they are permanent.

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

        for attempt in range(SIGENERGY_MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=SIGENERGY_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as response:
                        if response.status != 200:
                            self.log("Warn: SigenergyAPI: Auth request returned HTTP {} (attempt {}/{})".format(response.status, attempt + 1, SIGENERGY_MAX_RETRIES))
                            if attempt < SIGENERGY_MAX_RETRIES - 1:
                                await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
                                continue
                            return None
                        try:
                            data = await response.json(content_type=None)
                        except Exception as e:
                            self.log("Warn: SigenergyAPI: Failed to decode auth response: {}".format(e))
                            if attempt < SIGENERGY_MAX_RETRIES - 1:
                                await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
                                continue
                            return None

                code = data.get("code", -1)
                if code != 0:
                    # Permanent API-level failure (e.g. wrong credentials) — do not retry
                    self.log("Warn: SigenergyAPI: Auth failed, code={}, msg={}".format(code, data.get("msg", "unknown")))
                    self.access_token = None
                    return None

                token_data = data.get("data", {})
                if isinstance(token_data, str):
                    try:
                        token_data = json.loads(token_data)
                    except Exception as e:
                        self.log("Warn: SigenergyAPI: Failed to parse token data JSON: {}".format(e))
                        return None
                self.access_token = token_data.get("accessToken")
                expires_in = _safe_int(token_data.get("expiresIn", 43200), 43200)
                self.token_expires_at = now + expires_in
                self.log("SigenergyAPI: Token obtained, expires in {} s".format(expires_in))
                return self.access_token

            except asyncio.TimeoutError:
                self.log("Warn: SigenergyAPI: Auth request timed out (attempt {}/{})".format(attempt + 1, SIGENERGY_MAX_RETRIES))
                if attempt < SIGENERGY_MAX_RETRIES - 1:
                    await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
            except Exception as e:
                self.log("Warn: SigenergyAPI: Exception during authentication: {}".format(e))
                if attempt < SIGENERGY_MAX_RETRIES - 1:
                    await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)

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

        self.log("Requesting {} {} with params={} json={}".format(method, path, params, json_data))

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
                            record_api_call("sigenergy", False, "auth_error")
                            self.access_token = None
                            token = await self.get_access_token()
                            if not token:
                                return None
                            headers["Authorization"] = "Bearer {}".format(token)
                            continue

                        if response.status not in (200, 201):
                            self.log("Warn: SigenergyAPI: HTTP {} for {} {}".format(response.status, method, path))
                            record_api_call("sigenergy", False, "server_error")
                            if attempt < retries - 1:
                                await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
                                continue
                            return None

                        try:
                            body = await response.json(content_type=None)
                        except Exception as e:
                            self.log("Warn: SigenergyAPI: Failed to decode response from {}: {}".format(path, e))
                            return None

                        self.log("SigenergyAPI: Response from {} {}: {}".format(method, path, body))

                        code = body.get("code", -1)
                        if code != 0:
                            self._last_api_code = code
                            self.log("Warn: SigenergyAPI: API error code={} msg={} for {}".format(code, body.get("msg", ""), path))
                            record_api_call("sigenergy", False, "server_error")
                            if code == SIGENERGY_CODE_ACCESS_RESTRICTION:
                                # Rate-limited — exponential backoff then retry
                                wait = SIGENERGY_RATE_LIMIT_BACKOFF[min(attempt, len(SIGENERGY_RATE_LIMIT_BACKOFF) - 1)]
                                self.log("Warn: SigenergyAPI: Rate limited (1201) — waiting {}s before retry (attempt {}/{})" .format(wait, attempt + 1, retries))
                                if attempt < retries - 1:
                                    await asyncio.sleep(wait)
                                    continue
                            return None

                        data = body.get("data")
                        # Some Sigenergy endpoints double-encode the data field as a JSON string
                        if isinstance(data, str):
                            try:
                                data = json.loads(data)
                            except Exception:
                                pass  # Leave as string if it's not valid JSON
                        # Some endpoints return a list whose items are also JSON strings
                        if isinstance(data, list):
                            decoded = []
                            for item in data:
                                if isinstance(item, str):
                                    try:
                                        item = json.loads(item)
                                    except Exception:
                                        pass
                                decoded.append(item)
                            data = decoded
                        record_api_call("sigenergy")
                        return _SIGENERGY_OK if data is None else data

            except asyncio.TimeoutError:
                self.log("Warn: SigenergyAPI: Timeout on {} {} (attempt {}/{})".format(method, path, attempt + 1, retries))
                record_api_call("sigenergy", False, "connection_error")
                if attempt < retries - 1:
                    await asyncio.sleep(SIGENERGY_COMMAND_RETRY_DELAY)
            except Exception as e:
                self.log("Warn: SigenergyAPI: Exception on {} {}: {}\n{}".format(method, path, e, traceback.format_exc()))
                record_api_call("sigenergy", False, "connection_error")
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

        self.log("SigenergyAPI: System list raw data type={} value={}".format(type(data).__name__, repr(data)[:200]))

        # data may be a JSON string (some Sigenergy endpoints double-encode), a list, or a dict
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as e:
                self.log("Warn: SigenergyAPI: Failed to parse system list JSON string: {}".format(e))
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
        for device in devices:
            if isinstance(device, dict) and isinstance(device.get("attrMap"), str):
                try:
                    device["attrMap"] = json.loads(device["attrMap"])
                except Exception:
                    pass
        self.log("SigenergyAPI: System {} has {} device(s)".format(system_id, len(devices)))
        return True

    def _get_inverter_serial(self, system_id):
        """Return the serial number of the first Inverter (or AIO) device for a system.

        Args:
            system_id: Sigenergy system unique identifier.

        Returns:
            Serial number string, or None if not found.
        """
        for device in self.devices.get(system_id, []):
            dt = device.get("deviceType", "")
            if dt in (SIGENERGY_DEVICE_INVERTER, SIGENERGY_DEVICE_AIO):
                return device.get("serialNumber")
        return None

    async def fetch_inverter_realtime(self, system_id):
        """Fetch realtime data from the inverter realtimeInfo endpoint.

        Endpoint: GET /openapi/systems/{systemId}/devices/{serialNumber}/realtimeInfo

        Maps device-level fields to the same dict format as fetch_energy_flow so
        that all downstream code (publish_system_entities, apply_controls) works
        unchanged.  Also updates daily_summary with pvEnergyDaily if present.

        Field sign conventions (realtimeInfo vs energyFlow):
          batPower:   realtimeInfo positive=discharging  → energyFlow positive=charging (negated)
          activePower: positive=generation/export        → gridPower positive=export (same)
          loadPower is derived as: pv + battery_discharge - grid_export

        Note: The API enforces a 5-minute access restriction per device, so this
        should only be called at SIGENERGY_POLL_INTERVAL intervals (default 5 min).

        Args:
            system_id: Sigenergy system unique identifier.

        Returns:
            True on success, False on failure.
        """
        serial = self._get_inverter_serial(system_id)
        if not serial:
            self.log("Warn: SigenergyAPI: No inverter device found for system {}".format(system_id))
            return False

        data = await self._request("GET", "/openapi/systems/{}/devices/{}/realtimeInfo".format(system_id, serial))
        if data is None:
            self.log("Warn: SigenergyAPI: Failed to fetch inverter realtime info for {}".format(system_id))
            return False

        # data has systemId, serialNumber, deviceType, realTimeInfo
        rt = data.get("realTimeInfo", data)
        if isinstance(rt, str):
            try:
                rt = json.loads(rt)
            except Exception:
                pass

        bat_soc = _safe_float(rt.get("batSoc", 0))
        # batPower: realtimeInfo convention is positive=discharging, negative=charging.
        # energyFlow convention (used everywhere else) is positive=charging, negative=discharging.
        # Negate to match the energyFlow convention.
        bat_power_kw = -_safe_float(rt.get("batPower", 0))
        pv_power_kw = _safe_float(rt.get("pvPower", 0))
        # activePower: positive=export (net generation to grid), negative=import.
        # Maps directly to gridPower in the energyFlow convention (positive=export).
        grid_power_kw = _safe_float(rt.get("activePower", 0))
        # Derive load: pv + battery_discharge - grid_export
        # battery_discharge = -bat_power_kw when bat_power_kw < 0 (bat_power_kw is now in charging-positive convention)
        battery_discharge_kw = max(0.0, -bat_power_kw)
        load_power_kw = max(0.0, pv_power_kw + battery_discharge_kw - grid_power_kw)

        flow = {
            "batterySoc": bat_soc,
            "batteryPower": bat_power_kw,
            "pvPower": pv_power_kw,
            "gridPower": grid_power_kw,
            "loadPower": load_power_kw,
            "evPower": 0.0,
        }
        self.energy_flow[system_id] = flow

        # Update daily summary from pvEnergyDaily if present
        pv_daily = rt.get("pvEnergyDaily")
        if pv_daily is not None:
            if system_id not in self.daily_summary:
                self.daily_summary[system_id] = {}
            self.daily_summary[system_id]["dailyPowerGeneration"] = _safe_float(pv_daily)

        self.log("SigenergyAPI: System {} realtimeInfo — SOC {:.0f}% battery {:.2f}kW pv {:.2f}kW grid {:.2f}kW load {:.2f}kW".format(
            system_id, bat_soc, bat_power_kw, pv_power_kw, grid_power_kw, load_power_kw))
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

        Example data:
        {
            "code": 0,
            "msg": "success",
            "data": {
                "pvPower": 10.1,
                "gridPower": 10.1,
                "evPower": 0,
                "loadPower": 0,
                "heatPumpPower": 0,
                "batteryPower": 0,
                "batterySoc": 100
            }
        }
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
        self.log("SigenergyAPI: System {} current operating mode: {}".format(system_id, mode_int))
        return True

    # -----------------------------------------------------------------------
    # Control commands
    # -----------------------------------------------------------------------

    async def set_operating_mode(self, system_id, mode_int):
        """Set the energy storage operating mode via MQTT (V1 endpoint).

        Publishes to openapi/instruction/mode with the mode name string from
        SIGENERGY_MODE_API_NAMES rather than the integer used by the REST endpoint.

        Args:
            system_id: Sigenergy system unique identifier.
            mode_int: Operating mode integer (SIGENERGY_MODE_MSC/FFG/VPP).

        Returns:
            True on success, False on failure.
        """
        mode_name = SIGENERGY_MODE_API_NAMES.get(mode_int)
        if mode_name is None:
            self.log("Warn: SigenergyAPI: Unknown operating mode int {} for system {}".format(mode_int, system_id))
            return False
        token = await self.get_access_token()
        if not token:
            self.log("Warn: SigenergyAPI: No access token for MQTT mode switch")
            return False
        payload = {
            "accessToken": token,
            "systemId": system_id,
            "mode": mode_name,
        }
        ok = await self._publish_mqtt(SIGENERGY_MQTT_TOPIC_MODE, payload)
        if not ok:
            self.log("Warn: SigenergyAPI: Failed to set operating mode {} ({}) for system {}".format(mode_name, mode_int, system_id))
            return False
        self.log("SigenergyAPI: Operating mode set to {} ({}) for system {}".format(mode_name, mode_int, system_id))
        return True

    async def onboard_systems(self, system_ids):
        """Onboard one or more systems into the Sigenergy platform.

        Calls POST /openapi/board/onboard with a batch of system IDs.
        Inspects per-system result codes and logs the outcome.

        Args:
            system_ids: A single system ID string or a list of system ID strings.

        Returns:
            True on success, None if pending user approval (code 1116), False on failure.
        """
        if isinstance(system_ids, str):
            system_ids = [system_ids]
        self.log("SigenergyAPI: Onboarding systems: {}".format(system_ids))
        result = await self._request("POST", "/openapi/board/onboard", json_data=system_ids)

        # Extract the outcome code: prefer codeList[0] from the first per-system result dict,
        # fall back to the API-level error code set by _request.
        code = self._last_api_code
        if isinstance(result, list) and result:
            item = result[0]
            if isinstance(item, dict) and not item.get("result", True):
                item_codes = item.get("codeList", [])
                if item_codes:
                    code = item_codes[0]

        if code == SIGENERGY_CODE_SYSTEM_PENDING_REVIEW:
            self.log("Warn: SigenergyAPI: Onboard for {} pending user approval in the Sigenergy app — approve to enable controls".format(system_ids))
            return None
        if code in (SIGENERGY_CODE_IN_OTHER_VPP, SIGENERGY_CODE_IN_OTHER_VPP_EVERGEN):
            self.log("Warn: SigenergyAPI: Onboard failed — system {} is registered to another VPP (code={})".format(system_ids, code))
            return False
        if code == SIGENERGY_CODE_SOFTWARE_NO_VPP:
            self.log("Warn: SigenergyAPI: Onboard failed — system {} firmware does not support VPP (code=1105)".format(system_ids))
            return False
        if result is None:
            self.log("Warn: SigenergyAPI: Onboard request failed for systems {} (code={})".format(system_ids, code))
            return False

        self.log("SigenergyAPI: Onboard accepted for {}: {}".format(system_ids, result))
        return True

    async def offboard_systems(self, system_ids):
        """Offboard (remove) one or more systems from the Sigenergy platform.

        Calls POST /openapi/board/offboard with a batch of system IDs.

        Args:
            system_ids: A single system ID string or a list of system ID strings.

        Returns:
            List of per-system result dicts on success (may be empty), or None on failure.
        """
        if isinstance(system_ids, str):
            system_ids = [system_ids]
        payload = system_ids
        self.log("SigenergyAPI: Offboarding systems: {}".format(system_ids))
        result = await self._request("POST", "/openapi/board/offboard", json_data=payload)
        if result is None:
            self.log("Warn: SigenergyAPI: Offboard request failed for systems {}".format(system_ids))
            return None
        self.log("SigenergyAPI: Offboard completed for {}: {}".format(system_ids, result))
        return result

    def _build_tls_context(self):
        """Build an SSL context for the MQTT connection.

        Uses ca_cert if provided (PEM text, for custom/self-signed CA), otherwise the
        system default trust store.  Loads client_cert + client_key PEM text when both
        are provided (mutual TLS), writing them to temporary files once and caching the
        resulting SSLContext for all subsequent calls.

        Returns:
            ssl.SSLContext ready for use with aiomqtt.
        """
        if self._tls_context is not None:
            return self._tls_context

        if self.ca_cert:
            tls_context = ssl.create_default_context()
            tls_context.load_verify_locations(cadata=self.ca_cert)
            # Relax strict RFC 5280 key-usage enforcement; Sigenergy's CA cert
            # may not include the keyUsage/basicConstraints extensions that
            # Python 3.14+ enforces by default.
            tls_context.verify_flags = ssl.VERIFY_DEFAULT
        else:
            tls_context = ssl.create_default_context()
        if self.client_cert and self.client_key:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f_cert:
                f_cert.write(self.client_cert)
                cert_path = f_cert.name
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f_key:
                f_key.write(self.client_key)
                key_path = f_key.name
            try:
                tls_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            finally:
                os.unlink(cert_path)
                os.unlink(key_path)
        self._tls_context = tls_context
        return tls_context

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
            tls_context = self._build_tls_context()
            async with aiomqtt.Client(
                hostname=self.mqtt_host,
                port=self.mqtt_port,
                username=self.app_key,
                password=self.access_token,
                tls_context=tls_context,
                keepalive=30,
            ) as client:
                await client.publish(topic, payload=json.dumps(payload_dict), qos=1)
            self.log("SigenergyAPI: MQTT published to {} - {}".format(topic, payload_dict))
            return True
        except Exception as e:
            self.log("Warn: SigenergyAPI: MQTT publish to {} failed: {}".format(topic, e))
            return False

    async def send_battery_command(
        self,
        system_id,
        active_mode,
        duration_minutes,
        charging_power_kw=None,
        pv_power_kw=None,
        max_sell_power_kw=None,
        max_purchase_power_kw=None,
        charge_priority_type=None,
        discharge_priority_type=None,
    ):
        """Send a battery command via MQTT to the Sigenergy broker.

        Publishes to the MQTT topic ``openapi/instruction/command``.  A fresh
        access token is obtained if needed before building the payload.

        Args:
            system_id: Sigenergy system unique identifier.
            active_mode: One of the SIGENERGY_ACTIVE_MODE_* string constants.
            duration_minutes: Command duration in minutes (max ~720).
            charging_power_kw: Max energy storage charging/discharging power (kW).
            pv_power_kw: Max photovoltaic charging power (kW).
            max_sell_power_kw: Max export power to the grid (kW).
            max_purchase_power_kw: Max purchase power from the grid (kW).
            charge_priority_type: Charging priority enum string ('PV' or 'GRID').
            discharge_priority_type: Discharging priority enum string ('PV' or 'BATTERY').

        Returns:
            True on success, False on failure.

        Payload fields:
            Name                    Type    Required    Description
            accessToken             String  Yes         Authorization token obtained from Chapter 2
            systemId                String  Yes         Unique code of the power station
            activeMode              String  Yes         System active mode
            startTime               Long    Yes         Command start time, in seconds
            duration                Integer Yes         Command duration, in minutes
            chargingPower           Double  No          Max energy storage charging/discharging power (KW)
            pvPower                 Double  No          Max photovoltaic charging power (KW)
            maxSellPower            Double  No          Max export power to the grid (KW)
            maxPurchasePower        Double  No          Max purchase power from the grid (KW)
            chargePriorityType      Enum    No          Charging priority (PV/GRID)
            dischargePriorityType   Enum    No          Discharging priority (PV/BATTERY)

        """
        # De-duplication: skip if identical command sent within the last 5 minutes
        command_hash = hash((active_mode, charging_power_kw, pv_power_kw, max_sell_power_kw, max_purchase_power_kw, charge_priority_type, discharge_priority_type))
        last_hash, last_start_time = self._last_battery_command.get(system_id, (None, 0))
        now_ts = int(time.time())
        if last_hash == command_hash and (now_ts - last_start_time) < 300:
            self.log("SigenergyAPI: Skipping duplicate battery command {} for system {} ({:.1f} min ago)".format(active_mode, system_id, (now_ts - last_start_time) / 60))
            return True

        token = await self.get_access_token()
        if not token:
            self.log("Warn: SigenergyAPI: No access token for MQTT battery command")
            return False

        payload = {
            "accessToken": token,
            "commands": [{
                "systemId": system_id,
                "activeMode": active_mode,
                "startTime": now_ts,
                "duration": int(duration_minutes)
            }]
        }
        if charging_power_kw is not None:
            payload["chargingPower"] = round(charging_power_kw, 2)
        if pv_power_kw is not None:
            payload["pvPower"] = round(pv_power_kw, 2)
        if max_sell_power_kw is not None:
            payload["maxSellPower"] = round(max_sell_power_kw, 2)
        if max_purchase_power_kw is not None:
            payload["maxPurchasePower"] = round(max_purchase_power_kw, 2)
        if charge_priority_type is not None:
            payload["chargePriorityType"] = charge_priority_type
        if discharge_priority_type is not None:
            payload["dischargePriorityType"] = discharge_priority_type

        self.log("SigenergyAPI: Sending MQTT battery command {} ({} min, {:.2f}kW) to system {}".format(
            active_mode, duration_minutes, charging_power_kw or 0.0, system_id))

        ok = await self._publish_mqtt(SIGENERGY_MQTT_TOPIC_COMMAND, payload)
        if ok:
            self._last_battery_command[system_id] = (command_hash, now_ts)
        return ok

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

        Prefers the batteryCapacity field from the system-list response (already in kWh).
        Falls back to summing ratedEnergy from individual Battery devices.
        The device-level ratedEnergy field is in Ah; multiply by the nominal pack voltage
        (SIGENERGY_BATTERY_NOMINAL_VOLTAGE_V = 28.8 V for an 8S LiFePO4 pack) to convert to kWh.
        """
        system_info = self.systems.get(system_id, {})
        capacity = _safe_float(system_info.get("batteryCapacity", 0))
        if capacity > 0:
            return capacity

        # Fallback: sum device-level ratedEnergy (Ah) converted to kWh
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_BATTERY:
                attr = device.get("attrMap", {})
                rated_ah = _safe_float(attr.get("ratedEnergy", 0))
                capacity += rated_ah * SIGENERGY_BATTERY_NOMINAL_VOLTAGE_V / 1000.0
        return capacity

    def _get_battery_max_power_kw(self, system_id):
        """Return the combined rated charge/discharge power in kW for a system."""
        # Prefer device-level ratedChargePower
        power = 0.0
        inverter_power = self._get_inverter_max_power_kw(system_id)
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_BATTERY:
                attr = device.get("attrMap", {})
                power += _safe_float(attr.get("ratedChargePower", 0))

        # Fallback: inverter rated power
        # Also cap battery power at inverter power
        if power > 0:
            return min(power, inverter_power) if inverter_power > 0 else power
        else:
            return inverter_power

    def _get_inverter_max_power_kw(self, system_id):
        """Return the combined inverter rated active power in kW."""
        power = 0.0
        for device in self.devices.get(system_id, []):
            if device.get("deviceType") == SIGENERGY_DEVICE_INVERTER:
                attr = device.get("attrMap", {})
                power += _safe_float(attr.get("ratedActivePower", 0))
        return power

    # -----------------------------------------------------------------------
    # MQTT message handlers
    # -----------------------------------------------------------------------

    def _handle_mqtt_period(self, system_id, value_dict):
        """Handle a Sigenergy ``openapi/period`` MQTT message.

        Overwrites ``self.energy_flow[system_id]`` with fresh real-time data.
        The ``period`` message is broadcast every ~5 s by the broker and carries
        inverter and storage power/SOC values.

        Field sign convention matches the REST energyFlow convention used
        throughout the rest of the code:
          batteryPower — positive = charging, negative = discharging
          gridPower    — positive = export, negative = import
          pvPower      — always positive
          loadPower    — derived as ``pvPower − batteryPower − gridPower``

        Example message:
        {
            "PV power": "0.0",
            "gridPhaseCReactivePowerVar": "0.0",
            "inverterReactivePowerVar": "9.0",
            "inverterActivePowerW": "2681.0",
            "inverterPhaseBReactivePowerVar": "0.0",
            "inverterMaxAbsorptionActivePowerW": "12000.0",
            "onOffGridStatus": "0.0",
            "inverterPhaseAActivePowerW": "1345.0",
            "inverterPhaseBActivePowerW": "0.0",
            "gridActivePowerW": "3.0",
            "inverterMaxFeedInActivePowerW": "12000.0",
            "inverterPhaseAReactivePowerVar": "22.0",
            "inverterMaxFeedInReactivePowerVar": "7200.0",
            "storageChargeCapacityWh": "9520.0",
            "storageDischargeCapacityWh": "37410.0",
            "gridPhaseBReactivePowerVar": "0.0",
            "gridPhaseAActivePowerW": "3.0",
            "storageChargeDischargePowerW": "-2927.0",
            "operationalMode": "6.0",
            "storageSOC%": "79.7",
            "systemStatus": "1.0",
            "gridReactivePowerVar": "-238.0",
            "inverterMaxAbsorptionReactivePowerVar": "7200.0",
            "gridPhaseAReactivePowerVar": "-257.0",
            "batteryMaxDischargePowerW": "36051.0",
            "batteryMaxChargePowerW": "22032.0"
        }
        Args:
            system_id: Sigenergy system identifier extracted from the MQTT topic.
            value_dict: ``value`` sub-dict from the parsed MQTT payload (string→string).
        """
        # Note: storageChargeDischargePowerW is negative when discharging — same
        # sign convention as the REST batteryPower field (positive=charging).
        bat_power_kw = _safe_float(value_dict.get("storageChargeDischargePowerW", 0)) / 1000.0
        pv_power_kw = _safe_float(value_dict.get("PV power", 0)) / 1000.0
        grid_power_kw = _safe_float(value_dict.get("gridActivePowerW", 0)) / 1000.0
        load_power_kw = pv_power_kw - bat_power_kw - grid_power_kw

        flow = {
            "batterySoc": _safe_float(value_dict.get("storageSOC%", 0)),
            "batteryPower": bat_power_kw,
            "pvPower": pv_power_kw,
            "gridPower": grid_power_kw,
            "loadPower": max(0.0, load_power_kw),
            "evPower": 0.0,
            "inverterPower": _safe_float(value_dict.get("inverterActivePowerW", 0)) / 1000.0,
        }
        flow_status = {
            "chargeCapacity": _safe_float(value_dict.get("storageChargeCapacityWh", 0)) / 1000.0,
            "dischargeCapacity": _safe_float(value_dict.get("storageDischargeCapacityWh", 0)) / 1000.0,
            "ratedChargePower": _safe_float(value_dict.get("batteryMaxChargePowerW", 0)) / 1000.0,
            "ratedDischargePower": _safe_float(value_dict.get("batteryMaxDischargePowerW", 0)) / 1000.0,
            "operationalMode": _safe_float(value_dict.get("operationalMode", 0)),
            "systemStatus": _safe_float(value_dict.get("systemStatus", 0)),
        }
        self.energy_flow[system_id] = flow
        self.system_status[system_id] = flow_status
        if "operationalMode" in value_dict:
            self.current_mode[system_id] = int(_safe_float(value_dict["operationalMode"]))
        self.log(
            "SigenergyAPI: MQTT period {}: SOC {:.0f}% bat {:.2f}kW pv {:.2f}kW grid {:.2f}kW load {:.2f}kW".format(
                system_id, flow["batterySoc"], bat_power_kw, pv_power_kw, grid_power_kw, flow["loadPower"]
            )
        )

    def _handle_mqtt_change(self, system_id, value_dict):
        """Handle a Sigenergy ``openapi/change`` MQTT message.

        Updates ``self.controls[system_id]`` with the SOC limit values and
        updates capacity/power fields in ``self.systems[system_id]``.

        The ``change`` topic fires whenever the inverter's configuration
        changes (e.g. via the Sigenergy app or a remote API command).

        Per the user's clarification:
          backupCutOffSOC%   → reserve (backup/emergency minimum)
          chargeCutOffSOC%   → charge target_soc
          dischargeCutOffSOC% → export target_soc (minimum discharge floor)


        example message:
        {
            "batteryRatedChargePowerW": "22000.0",
            "batteryRatedCapabilityWh": "45200.0",
            "backupCutOffSOC%": "15.0",
            "inverterMaxAbsorptionPowerW": "12000.0",
            "peakShavingStatus": "off",
            "stormWatchStatus": "off",
            "batteryRatedDischargePowerW": "24000.0",
            "inverterMaxActivePowerW": "12000.0",
            "dischargeCutOffSOC%": "5.0",
            "chargeCutOffSOC%": "100.0",
            "peakShavingCutOffSOC%": "0.0",
            "inverterMaxApprentPowerVar": "12000.0",
            "gridMaxBackfeedPowerW": "5000.0"
        }

        Args:
            system_id: Sigenergy system identifier.
            value_dict: ``value`` sub-dict from the parsed MQTT payload.
        """
        # Update controls
        if system_id not in self.controls:
            self.controls[system_id] = {}

        reserve = _safe_float(value_dict.get("backupCutOffSOC%", None), None)
        if reserve is not None:
            self.controls[system_id]["reserve"] = reserve

        charge_target = _safe_float(value_dict.get("chargeCutOffSOC%", None), None)
        if charge_target is not None:
            if "charge" not in self.controls[system_id]:
                self.controls[system_id]["charge"] = {}
            self.controls[system_id]["charge"]["target_soc"] = charge_target

        export_target = _safe_float(value_dict.get("dischargeCutOffSOC%", None), None)
        if export_target is not None:
            if "export" not in self.controls[system_id]:
                self.controls[system_id]["export"] = {}
            self.controls[system_id]["export"]["target_soc"] = export_target

        # Update system capacity / power limits
        if system_id not in self.systems:
            self.systems[system_id] = {}

        capacity_wh = _safe_float(value_dict.get("batteryRatedCapabilityWh", None))
        if capacity_wh:
            self.systems[system_id]["batteryCapacity"] = capacity_wh / 1000.0

        for mqtt_field, sys_key in (
            ("batteryRatedChargePowerW", "ratedChargePower"),
            ("batteryRatedDischargePowerW", "ratedDischargePower"),
            ("inverterMaxActivePowerW", "ratedActivePower"),
            ("gridMaxBackfeedPowerW", "gridMaxBackfeedPower"),
        ):
            val = _safe_float(value_dict.get(mqtt_field, None))
            if val:
                self.systems[system_id][sys_key] = val / 1000.0

        self.log(
            "SigenergyAPI: MQTT change {}: reserve={}% charge_target={}% export_target={}%".format(
                system_id, reserve, charge_target, export_target
            )
        )

    def _handle_mqtt_alarm(self, system_id, payload_list):
        """Handle a Sigenergy ``openapi/alarm`` MQTT message.

        Args:
            system_id: Sigenergy system identifier.
            payload_list: Parsed payload list from the MQTT message.
        """
        self.log("Warn: SigenergyAPI: MQTT alarm for system {}: {}".format(system_id, payload_list))

    async def _mqtt_listener_loop(self):
        """Persistent MQTT listener coroutine.

        Runs for the lifetime of the component (until ``self.api_stop`` is set).
        On each (re)connect cycle:
          1. Refreshes the access token.
          2. Opens a TLS MQTT connection to the Sigenergy broker.
          3. Subscribes to wildcard topics for change, period and alarm data.
          4. Publishes Sigenergy application-level subscription requests.
          5. Dispatches incoming messages to the appropriate handler.
          6. Publishes updated HA entities when data changes.
          7. Reconnects automatically on any error or clean broker disconnect.

        This coroutine is started as an ``asyncio.Task`` from ``run()`` after
        first successful authentication, and cancelled in ``final()``.
        """
        if not HAS_AIOMQTT:
            self.log("Error: SigenergyAPI: aiomqtt is not installed — MQTT listener cannot start")
            return

        reconnect_delay = 5
        attempt = 0
        system_id_list = list(self.systems.keys()) if self.systems else []
        # Build per-app-key wildcard topics
        topics = [
            SIGENERGY_MQTT_TOPIC_CHANGE.format(app_key=self.app_key, system_id="#"),
            SIGENERGY_MQTT_TOPIC_PERIOD.format(app_key=self.app_key, system_id="#"),
            SIGENERGY_MQTT_TOPIC_ALARM.format(app_key=self.app_key, system_id="#"),
        ]

        while not self.api_stop:
            attempt += 1
            self.log("SigenergyAPI: MQTT listener connecting (attempt #{}) ...".format(attempt))
            try:
                token = await self.get_access_token()
                if not token:
                    self.log("Warn: SigenergyAPI: MQTT token refresh failed; retrying in {}s".format(reconnect_delay))
                    await asyncio.sleep(reconnect_delay)
                    continue

                # Refresh system list for subscription if not yet known
                if not system_id_list:
                    system_id_list = list(self.systems.keys())

                tls_context = self._build_tls_context()
                async with aiomqtt.Client(
                    hostname=self.mqtt_host,
                    port=SIGENERGY_MQTT_PORT,
                    username=self.app_key,
                    password=token,
                    tls_context=tls_context,
                    keepalive=60,
                ) as client:
                    self.log("SigenergyAPI: MQTT connected to {}:{}".format(self.mqtt_host, SIGENERGY_MQTT_PORT))
                    for topic in topics:
                        await client.subscribe(topic)

                    sub_payload = json.dumps({"accessToken": token, "systemIdList": system_id_list})
                    for sub_topic in ("openapi/subscription/period", "openapi/subscription/change", "openapi/subscription/alarm"):
                        await client.publish(sub_topic, payload=sub_payload, qos=1)
                    self.log("SigenergyAPI: MQTT subscriptions published for {} system(s)".format(len(system_id_list)))

                    async for message in client.messages:
                        if self.api_stop:
                            break

                        # Parse topic: openapi/{type}/{app_key}/{system_id}
                        topic_str = str(message.topic)
                        parts = topic_str.split("/")
                        # Expected: ['openapi', type, app_key, system_id]
                        if len(parts) < 4:
                            continue
                        msg_type = parts[1]   # change / period / alarm
                        msg_sid = parts[3]    # system ID

                        # Decode payload
                        raw = message.payload if isinstance(message.payload, (bytes, bytearray)) else str(message.payload).encode()
                        try:
                            payload = json.loads(raw.decode("utf-8", errors="replace"))
                        except (json.JSONDecodeError, ValueError):
                            self.log("Warn: SigenergyAPI: MQTT non-JSON payload on {}: {}".format(topic_str, raw[:120]))
                            continue

                        # Each message is a list of device-level entries; process each
                        entries = payload if isinstance(payload, list) else [payload]
                        for entry in entries:
                            entry_sid = entry.get("systemId", msg_sid)
                            self.last_mqtt_update[entry_sid] = time.time()
                            value_dict = entry.get("value", {})
                            self.log("SigenergyAPI: MQTT message on {} for system {}: type={} value={}".format(topic_str, entry_sid, msg_type, value_dict))
                            if msg_type == "period":
                                self._handle_mqtt_period(entry_sid, value_dict)
                                if self.api_started:
                                    await self.publish_system_entities(entry_sid)
                            elif msg_type == "change":
                                self._handle_mqtt_change(entry_sid, value_dict)
                                if self.api_started:
                                    await self.publish_controls(entry_sid)
                                    await self.publish_system_entities(entry_sid)
                            elif msg_type == "alarm":
                                self._handle_mqtt_alarm(entry_sid, entries)

                # Broker closed connection cleanly
                self.log("Warn: SigenergyAPI: MQTT connection closed by broker — reconnecting in {}s".format(reconnect_delay))
                await asyncio.sleep(reconnect_delay)

            except aiomqtt.MqttError as e:
                self.log("Warn: SigenergyAPI: MQTT error: {} — reconnecting in {}s".format(e, reconnect_delay))
                await asyncio.sleep(reconnect_delay)
            except asyncio.CancelledError:
                self.log("SigenergyAPI: MQTT listener cancelled")
                return
            except Exception as e:
                self.log("Warn: SigenergyAPI: MQTT unexpected error ({}): {} — reconnecting in {}s".format(type(e).__name__, e, reconnect_delay))
                await asyncio.sleep(reconnect_delay)

        self.log("SigenergyAPI: MQTT listener stopped")

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
        flow_status = self.system_status.get(system_id, {})
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

        # --- Operational mode (string) ---
        op_mode_int = int(_safe_float(flow_status.get("operationalMode", -1)))
        op_mode_str = SIGENERGY_MODE_NAMES.get(op_mode_int, "Unknown ({})".format(op_mode_int) if op_mode_int >= 0 else "Unknown")
        sys_status_int = int(_safe_float(flow_status.get("systemStatus", -1)))
        sys_status_str = SIGENERGY_SYSTEM_STATUS_NAMES.get(sys_status_int, "Unknown ({})".format(sys_status_int) if sys_status_int >= 0 else "Unknown")
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_operational_mode".format(self.prefix, slug),
            state=op_mode_str,
            attributes={
                "friendly_name": "Sigenergy {} Operational Mode".format(system_name),
                "mode_id": op_mode_int if op_mode_int >= 0 else None,
                "system_status": sys_status_str,
                "system_status_id": sys_status_int if sys_status_int >= 0 else None,
                "charge_capacity_kwh": flow_status.get("chargeCapacity", 0),
                "discharge_capacity_kwh": flow_status.get("dischargeCapacity", 0),
                "rated_charge_power_kw": flow_status.get("ratedChargePower", 0),
                "rated_discharge_power_kw": flow_status.get("ratedDischargePower", 0),
            },
            app="sigenergy",
        )

        # --- Last MQTT update time ---
        last_mqtt_ts = self.last_mqtt_update.get(system_id, 0)
        if last_mqtt_ts > 0:
            from datetime import timezone
            last_update_str = datetime.fromtimestamp(last_mqtt_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            last_update_str = "unknown"
        self.dashboard_item(
            "sensor.{}_sigenergy_{}_time".format(self.prefix, slug),
            state=last_update_str,
            attributes={
                "friendly_name": "Sigenergy {} Last Update".format(system_name),
                "icon": "mdi:clock",
                "state_class": "timestamp",
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
        self.set_arg("inverter_type", ["SIGCLOUD" for _ in range(num)])

        self.set_arg("soc_kw", ["sensor.{}_sigenergy_{}_battery_soc".format(self.prefix, s) for s in slugs])
        self.set_arg("soc_max", ["sensor.{}_sigenergy_{}_battery_capacity".format(self.prefix, s) for s in slugs])
        self.set_arg("battery_power", ["sensor.{}_sigenergy_{}_battery_power".format(self.prefix, s) for s in slugs])
        self.set_arg("battery_rate_max", ["sensor.{}_sigenergy_{}_battery_rate_max".format(self.prefix, s) for s in slugs])
        self.set_arg("inverter_limit", ["sensor.{}_sigenergy_{}_inverter_limit".format(self.prefix, s) for s in slugs])
        self.set_arg("pv_power", ["sensor.{}_sigenergy_{}_pv_power".format(self.prefix, s) for s in slugs])
        self.set_arg("grid_power", ["sensor.{}_sigenergy_{}_grid_power".format(self.prefix, s) for s in slugs])
        self.set_arg("load_power", ["sensor.{}_sigenergy_{}_load_power".format(self.prefix, s) for s in slugs])
        self.set_arg("pv_today", ["sensor.{}_sigenergy_{}_pv_today".format(self.prefix, s) for s in slugs])
        self.set_arg("inverter_time", ["sensor.{}_sigenergy_{}_time".format(self.prefix, s) for s in slugs])

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
            default = "00:00:00"
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
        elif field == "offboard":
            default = False
            field_type = "switch"

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
        for field in ("reserve", "offboard"):
            item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(system_id, None, field)
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

            for field in ("reserve", "offboard"):
                item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self._control_info(sid, None, field)
                value = self.controls[sid].get(field, default)
                attributes = {"friendly_name": friendly_name}
                if field_type == "switch":
                    value = "on" if value else "off"
                else:
                    if field_units:
                        attributes["unit_of_measurement"] = field_units
                    if min_value is not None:
                        attributes["min"] = min_value
                        attributes["max"] = max_value
                        attributes["step"] = 1
                self.dashboard_item(ha_name, state=value, attributes=attributes, app="sigenergy")

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
        elif field == "offboard":
            current = self.controls[system_id].get(field, False)
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

        if field == "offboard" and value is True:
            self.log("SigenergyAPI: Offboard toggle turned on for {} — offboarding".format(system_id))
            await self.offboard_systems(system_id)

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
        charge_start_str = self.controls[system_id].get("charge", {}).get("start_time", "00:00:00")
        charge_end_str = self.controls[system_id].get("charge", {}).get("end_time", "00:00:00")
        charge_target_soc = _safe_int(self.controls[system_id].get("charge", {}).get("target_soc", 100), 100)
        charge_rate_w = _safe_int(self.controls[system_id].get("charge", {}).get("rate", round(battery_max_kw * 1000)), round(battery_max_kw * 1000))
        export_enable = self.controls[system_id].get("export", {}).get("enable", False)
        export_start_str = self.controls[system_id].get("export", {}).get("start_time", "00:00:00")
        export_end_str = self.controls[system_id].get("export", {}).get("end_time", "00:00:00")
        export_target_soc = _safe_int(self.controls[system_id].get("export", {}).get("target_soc", 0), 0)
        export_rate_w = _safe_int(self.controls[system_id].get("export", {}).get("rate", round(battery_max_kw * 1000)), round(battery_max_kw * 1000))
        reserve_soc = _safe_int(self.controls[system_id].get("reserve", 10), 10)
        charge_power_kw = None
        charge_priority_type=None
        discharge_priority_type=None

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
                active_mode = SIGENERGY_ACTIVE_MODE_SELF_GRID
            else:
                new_mode = "export"
                active_mode = SIGENERGY_ACTIVE_MODE_DISCHARGE
                discharge_priority_type = "PV"
        elif charge_window and charge_start_dt and charge_end_dt:
            duration_min = max(1, int((charge_end_dt - now).total_seconds() / 60))
            effective_target = max(charge_target_soc, reserve_soc)
            if effective_target <= reserve_soc or abs(effective_target - battery_soc_pct) < 1:
                # Freeze charge — stay at current SOC
                new_mode = "freeze_charge"
                active_mode = SIGENERGY_ACTIVE_MODE_SELF
                charge_power_kw = 0
            elif effective_target < battery_soc_pct:
                # Target below current — go to eco
                new_mode = "eco"
                active_mode = SIGENERGY_ACTIVE_MODE_SELF
            else:
                new_mode = "charge"
                active_mode = SIGENERGY_ACTIVE_MODE_CHARGE
                charge_power_kw = charge_rate_w / 1000.0
                charge_priority_type = "PV"
        else:
            duration_min = 720
            new_mode = "eco"
            active_mode = SIGENERGY_ACTIVE_MODE_SELF

        duration_min = min(duration_min, 720)

        self.log("SigenergyAPI: Applying mode={} charge_power_kw={}kW duration={}min charge_priority_type={} discharge_priority_type={} to system {}".format(new_mode, "{:.2f}".format(charge_power_kw) if charge_power_kw is not None else "None", duration_min, charge_priority_type, discharge_priority_type, system_id))

        # Send battery command via MQTT — de-duplication handled inside send_battery_command
        return await self.send_battery_command(system_id, active_mode, duration_min, charging_power_kw=charge_power_kw, charge_priority_type=charge_priority_type, discharge_priority_type=discharge_priority_type)

    # -----------------------------------------------------------------------
    # VPP registration management
    # -----------------------------------------------------------------------

    async def _manage_vpp_registration(self, system_id, is_readonly, is_offboard=False):
        """Align the operating mode with the read-only and offboard switch settings.

        Assumes the system is already visible in self.systems (i.e. onboarded).
        Onboarding of missing systems is handled separately by the missing_ids
        block in run().

        Cases (offboard takes priority over readonly):
          offboard=True  + VPP active   → switch to MSC so the user's app regains control
          offboard=True  + VPP inactive → nothing to do (already out of VPP)
          readonly=True  + VPP active   → switch to MSC so the user's app regains control
          readonly=True  + VPP inactive → nothing to do
          readonly=False + VPP active   → nothing to do (ready for controls)
          readonly=False + VPP inactive → switch to VPP mode to enable controls

        Args:
            system_id: Sigenergy system unique identifier.
            is_readonly: Current state of the Predbat read-only switch.
            is_offboard: Current state of the per-system offboard toggle switch.

        Returns:
            True if the system is in VPP mode and controls can proceed, False otherwise.
        """
        in_vpp = self.current_mode.get(system_id) == SIGENERGY_MODE_VPP

        if is_offboard:
            return False

        if is_readonly and in_vpp:
            self.log("SigenergyAPI: Read-only mode active — switching system {} from VPP to MSC".format(system_id))
            await self.set_operating_mode(system_id, SIGENERGY_MODE_MSC)
            return False

        if not is_readonly and not in_vpp:
            self.log("SigenergyAPI: System {} is not in VPP mode — switching to VPP to enable controls".format(system_id))
            await self.set_operating_mode(system_id, SIGENERGY_MODE_VPP)
            return False  # current_mode will be updated by MQTT/REST on the next cycle

        return in_vpp

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
            if not self.system_id_filter:
                self.log("Warn: SigenergyAPI: No system_id configured — will use all authorised systems")
            token = await self.get_access_token()
            if not token:
                self.log("Warn: SigenergyAPI: Authentication failed — cannot proceed")
                return False
            await self.fetch_system_list()

            # For each expected system ID not yet visible, attempt onboarding
            missing_ids = self.system_id_filter - set(self.systems.keys()) if self.system_id_filter else set()
            for sid in missing_ids:
                slug = self._system_slug(sid)
                is_offboard_at_start = self.get_state_wrapper("switch.{}_sigenergy_{}_offboard".format(self.prefix, slug), default="off") == "on"
                if is_offboard_at_start:
                    self.log("SigenergyAPI: System {} offboard toggle is on — skipping onboard attempt".format(sid))
                    continue
                self.log("SigenergyAPI: System {} not found in authorised list — attempting onboard".format(sid))
                result = await self.onboard_systems([sid])
                if result is not True:
                    return False
                await self.fetch_system_list()

            if not self.systems:
                self.log("Warn: SigenergyAPI: No systems available after discovery, will retry")
                return False

            # Bootstrap current_mode via REST so VPP management can run immediately.
            # After this, MQTT period messages keep current_mode up to date each cycle.
            for sid in list(self.systems.keys()):
                await self.fetch_current_mode(sid)

        # Start (or restart) the background MQTT listener task after systems are known
        if self.systems and (self._mqtt_task is None or self._mqtt_task.done()):
            if self._mqtt_task is not None and self._mqtt_task.done():
                exc = self._mqtt_task.exception() if not self._mqtt_task.cancelled() else None
                if exc:
                    self.log("Warn: SigenergyAPI: MQTT listener task exited with error: {} — restarting".format(exc))
                else:
                    self.log("SigenergyAPI: MQTT listener task ended — restarting")
            self._mqtt_task = asyncio.ensure_future(self._mqtt_listener_loop())

        # Refresh device inventory periodically
        if first or seconds % SIGENERGY_DEVICE_POLL_INTERVAL == 0:
            for sid in list(self.systems.keys()):
                await self.fetch_device_list(sid)

        # VPP registration management — runs at startup and every 5 minutes.
        # Skips any system whose operating mode is not yet known (REST bootstrap
        # may have failed; MQTT will populate current_mode once it arrives).
        if first or seconds % SIGENERGY_POLL_INTERVAL == 0:
            is_readonly_vpp = self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"
            for sid in list(self.systems.keys()):
                if sid not in self.current_mode:
                    self.log("SigenergyAPI: Skipping VPP registration check for {} — operating mode not yet known".format(sid))
                    continue
                slug = self._system_slug(sid)
                is_offboard = self.get_state_wrapper("switch.{}_sigenergy_{}_offboard".format(self.prefix, slug), default="off") == "on"
                await self._manage_vpp_registration(sid, is_readonly_vpp, is_offboard)

        # Fetch controls from HA on first run only
        if first:
            for sid in list(self.systems.keys()):
                await self.fetch_controls(sid)
            await self.publish_controls()

        # Realtime data refresh — skip live power/SOC fetch when MQTT is providing fresh data
        if first or seconds % SIGENERGY_POLL_INTERVAL == 0:
            now_ts = time.time()
            for sid in list(self.systems.keys()):
                last_update = self.last_mqtt_update.get(sid, 0)
                mqtt_age = now_ts - last_update
                mqtt_fresh = last_update > 0 and mqtt_age < SIGENERGY_POLL_INTERVAL
                if mqtt_fresh:
                    self.log("SigenergyAPI: Skipping REST energy poll for {} (MQTT data {:.0f}s old)".format(sid, mqtt_age))
                else:
                    if not await self.fetch_inverter_realtime(sid):
                        await self.fetch_energy_flow(sid)
                # Always poll daily summary — not provided by MQTT
                await self.fetch_daily_summary(sid)

        # Publish entities
        if first or seconds % SIGENERGY_POLL_INTERVAL == 0:
            for sid in list(self.systems.keys()):
                await self.publish_system_entities(sid)

        # Automatic configuration
        if first and self.automatic:
            await self.automatic_config()

        # Apply controls
        is_readonly = self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"
        if self.enable_controls and not is_readonly:
            if first or seconds % 60 == 0:
                for sid in list(self.systems.keys()):
                    if self.current_mode.get(sid) != SIGENERGY_MODE_VPP:
                        self.log(
                            "Warn: SigenergyAPI: System {} is not in VPP mode ({}) — controls skipped until onboard is approved".format(
                                sid, SIGENERGY_MODE_NAMES.get(self.current_mode.get(sid, -1), "Unknown")
                            )
                        )
                        continue
                    await self.apply_controls(sid)
        else:
            if first:
                if is_readonly:
                    self.log("SigenergyAPI: Read-only mode active — controls skipped")
                else:
                    self.log("SigenergyAPI: Controls disabled")

        self.update_success_timestamp()
        return True

    async def final(self):
        """Cancel the background MQTT listener task on component shutdown."""
        if self._mqtt_task is not None and not self._mqtt_task.done():
            self.log("SigenergyAPI: Cancelling MQTT listener task")
            self._mqtt_task.cancel()
            try:
                await self._mqtt_task
            except (asyncio.CancelledError, Exception):
                pass
        self.log("SigenergyAPI: final() complete")


class MockBase:  # pragma: no cover
    """Mock base class for standalone testing."""

    def __init__(self, readonly=False):
        """Initialise mock base."""
        self.prefix = "predbat"
        self.local_tz = datetime.now().astimezone().tzinfo
        self.args = {}
        self.entities = {}
        # Pre-populate the read-only switch so get_state_wrapper returns the right value
        self.entities["switch.predbat_set_read_only"] = {"state": "on" if readonly else "off"}

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

    def get_arg(self, arg, default=None, indirect=False, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return arg default (mock always returns default)."""
        return default

    def set_arg(self, key, value):
        """Print auto-config arg assignment."""
        state = str(value)
        print("Set arg {} = {}".format(key, state))

    def update_success_timestamp(self):
        """No-op success timestamp update."""


async def test_sigenergy_api(app_key, app_secret, base_url, system_id, test_mode, action=None, mqtt_host=None, ca_cert=None, client_cert=None, client_key=None, readonly=False):  # pragma: no cover
    """Run one cycle of the Sigenergy API and optionally test a control mode or boarding action.

    Args:
        app_key: Sigenergy Application Key.
        app_secret: Sigenergy Application Secret.
        base_url: API base URL.
        system_id: Optional system ID filter string (required for onboard/offboard).
        test_mode: One of 'eco', 'charge', 'freeze_charge', 'export', 'freeze_export', or None.
        action: One of 'onboard', 'offboard', or None.
        readonly: When True simulate the Predbat read-only switch being on.
    """
    print("\n{}".format("=" * 60))
    print("Testing Sigenergy Cloud API")
    print("Base URL: {}".format(base_url))
    print("App Key: {}...".format(app_key[:10] if len(app_key) >= 10 else app_key))
    if system_id:
        print("System ID filter: {}".format(system_id))
    if test_mode:
        print("Test mode: {}".format(test_mode))
    if action:
        print("Action: {}".format(action))
    if readonly:
        print("Read-only: ON")
    print("{}\n".format("=" * 60))

    mock_base = MockBase(readonly=readonly)

    sig = SigenergyAPI(
        mock_base,
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        mqtt_host=mqtt_host,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        system_id=system_id,
        automatic=True,
        enable_controls=(test_mode is not None),
    )

    # For boarding actions we only need the token, not a full system scan
    if action in ("onboard", "offboard"):
        if not system_id:
            print("x --system-id is required for --{}\n".format(action))
            return 1
        token = await sig.get_access_token()
        if not token:
            print("x Authentication failed")
            return 1
        print("+ Authentication successful")
        if action == "onboard":
            board_result = await sig.onboard_systems(system_id)
        else:
            board_result = await sig.offboard_systems(system_id)
        if board_result is not True:
            print("x {} failed or pending for system {}".format(action.capitalize(), system_id))
            return 1
        print("+ {} successful for system {}".format(action.capitalize(), system_id))
        return 0

    if not test_mode:
        result = await sig.run(first=True, seconds=0)
        if not result:
            print("x Initialisation failed")
            return 1
        print("+ Initialisation successful")

    if test_mode:
        await sig.fetch_system_list()
        sid = list(sig.systems.keys())[0]
        #await sig.fetch_current_mode(sid)
        flow = sig.energy_flow.get(sid, {})
        battery_soc_pct = _safe_float(flow.get("batterySoc", 50))
        now = datetime.now(sig.local_tz)

        print("\n{}".format("=" * 60))
        print("Testing control mode: {}".format(test_mode))
        print("System ID: {}  SOC: {:.0f}%".format(sid, battery_soc_pct))
        print("{}\n".format("=" * 60))

        def _window(offset_start_min, offset_end_min):
            """Return HH:MM:SS strings offset from now."""
            s = (now + timedelta(minutes=offset_start_min)).strftime("%H:%M:%S")
            e = (now + timedelta(minutes=offset_end_min)).strftime("%H:%M:%S")
            return s, e

        battery_max_w = round(sig._get_battery_max_power_kw(sid) * 1000) or 5000

        if test_mode == "eco":
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": "23:00:00", "end_time": "23:30:00", "enable": False, "target_soc": 100, "rate": battery_max_w},
                "export": {"start_time": "23:30:00", "end_time": "23:59:00", "enable": False, "target_soc": 10, "rate": battery_max_w},
            }
            print("+ Configured for ECO mode (no active windows)")

        elif test_mode == "charge":
            cs, ce = _window(-30, 120)
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": cs, "end_time": ce, "enable": True, "target_soc": 95, "rate": battery_max_w},
                "export": {"start_time": "23:30:00", "end_time": "23:59:00", "enable": False, "target_soc": 10, "rate": battery_max_w},
            }
            print("+ Configured for CHARGE mode ({} - {}, target 95%)".format(cs, ce))

        elif test_mode == "freeze_charge":
            cs, ce = _window(-30, 120)
            target = round(battery_soc_pct)  # same as current = freeze
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": cs, "end_time": ce, "enable": True, "target_soc": target, "rate": battery_max_w},
                "export": {"start_time": "23:30:00", "end_time": "23:59:00", "enable": False, "target_soc": 10, "rate": battery_max_w},
            }
            print("+ Configured for FREEZE CHARGE mode ({} - {}, target=current {:.0f}%)".format(cs, ce, battery_soc_pct))

        elif test_mode == "export":
            es, ee = _window(-30, 120)
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": "23:00:00", "end_time": "23:30:00", "enable": False, "target_soc": 100, "rate": battery_max_w},
                "export": {"start_time": es, "end_time": ee, "enable": True, "target_soc": 15, "rate": battery_max_w},
            }
            print("+ Configured for EXPORT mode ({} - {}, target 15%)".format(es, ee))

        elif test_mode == "freeze_export":
            es, ee = _window(-30, 120)
            target = min(100, round(battery_soc_pct) + 10)  # above current = freeze
            sig.controls[sid] = {
                "reserve": 10,
                "charge": {"start_time": "23:00:00", "end_time": "23:30:00", "enable": False, "target_soc": 100, "rate": battery_max_w},
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


async def test_mqtt_connection(app_key, app_secret, base_url, system_id=None, topic_filter=None, ca_cert=None, client_cert=None, client_key=None):  # pragma: no cover
    """Connect to the Sigenergy MQTT broker, request push data, and print all incoming messages.

    Delegates to ``SigenergyAPI._mqtt_listener_loop()`` which is the single
    implementation of the reconnect/subscribe/dispatch logic used by both the
    test CLI and the live component.  Received messages are printed to stdout
    via ``SigenergyAPI.log()`` (which maps to ``print()`` in the mock base).

    Args:
        app_key: Sigenergy Application Key.
        app_secret: Sigenergy Application Secret.
        base_url: REST API base URL (MQTT host is derived from this).
        system_id: Optional system ID (or comma-separated list) to subscribe to.
        topic_filter: Unused in this implementation; wildcard topics are built
                      automatically from the app_key.
        ca_cert: PEM text of the CA certificate for TLS verification.
        client_cert: PEM text of the client certificate for mutual TLS.
        client_key: PEM text of the client private key for mutual TLS.

    Returns:
        0 on clean exit, 1 on error.
    """
    if not HAS_AIOMQTT:
        print("x aiomqtt is not installed.  Run: pip install aiomqtt")
        return 1

    print("\n{}".format("=" * 60))
    print("Sigenergy MQTT test mode")
    print("Base URL : {}".format(base_url))
    print("App Key  : {}...".format(app_key[:10] if len(app_key) >= 10 else app_key))
    print("{}\n".format("=" * 60))

    mock_base = MockBase()
    sig = SigenergyAPI(
        mock_base,
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        system_id=system_id,
    )

    # Authenticate and discover systems so the listener knows what to subscribe to
    token = await sig.get_access_token()
    if not token:
        print("x Authentication failed")
        return 1
    print("+ Authentication successful")
    print("+ MQTT broker: {}:{}".format(sig.mqtt_host, SIGENERGY_MQTT_PORT))
    if sig.ca_cert:
        print("+ CA cert    : {}".format(sig.ca_cert))
    if sig.client_cert:
        print("+ Client cert: {}".format(sig.client_cert))

    if system_id:
        # Populate systems dict from the provided IDs so the listener uses them
        for sid in [s.strip() for s in system_id.split(",") if s.strip()]:
            if sid not in sig.systems:
                sig.systems[sid] = {}
    else:
        print("+ No --system-id provided; scanning for authorised systems ...")
        await sig.fetch_system_list()
        if not sig.systems:
            print("x No systems found; cannot build subscription request")
            return 1
        print("+ Found systems: {}".format(list(sig.systems.keys())))

    print("+ Topics: change, period, alarm for app_key wildcard")
    print("+ Waiting for messages (Ctrl+C to stop) ...\n")

    # Run the shared listener loop — it handles reconnect, subscribe, dispatch
    # and prints via self.log() → MockBase.log() → print()
    sig.api_stop = False
    try:
        await sig._mqtt_listener_loop()
    except KeyboardInterrupt:
        sig.api_stop = True
        print("\n[{}] Ctrl+C received".format(datetime.now().strftime("%H:%M:%S")))


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
    parser.add_argument("--mqtt-host", default=None, help="Override MQTT broker hostname (default: derived from --base-url)")
    parser.add_argument("--mqtt-topic", default=None, help="MQTT topic filter for --mqtt-test; omit to use per-app-key topic patterns")
    parser.add_argument("--cert-dir", default=None, help="Directory containing ca.pem, client.pem and client.key TLS certificate files")
    parser.add_argument("--ca-cert", default=None, help="Path to CA certificate PEM file (overrides --cert-dir/ca.pem)")
    parser.add_argument("--client-cert", default=None, help="Path to client certificate PEM file (overrides --cert-dir/client.pem)")
    parser.add_argument("--client-key", default=None, help="Path to client private key file (overrides --cert-dir/client.key)")
    parser.add_argument("--readonly", action="store_true", help="Simulate read-only mode (switch.predbat_set_read_only = on)")

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--onboard",
        action="store_true",
        help="Onboard the system specified by --system-id",
    )
    action_group.add_argument(
        "--offboard",
        action="store_true",
        help="Offboard (remove) the system specified by --system-id",
    )
    action_group.add_argument(
        "--mqtt-test",
        action="store_true",
        help="Connect to the MQTT broker and print all received messages until Ctrl+C",
    )

    args = parser.parse_args()

    # Resolve TLS certificate file paths — individual flags override cert-dir — then read contents
    import os

    def _read_cert(path):
        """Read a certificate or key file and return its text content, or None."""
        if not path:
            return None
        try:
            with open(path) as f:
                return f.read()
        except OSError as e:
            print("Error: Cannot read TLS file {}: {}".format(path, e))
            raise SystemExit(1)

    ca_cert_path = args.ca_cert or (os.path.join(args.cert_dir, "ca.pem") if args.cert_dir else None)
    client_cert_path = args.client_cert or (os.path.join(args.cert_dir, "client.pem") if args.cert_dir else None)
    client_key_path = args.client_key or (os.path.join(args.cert_dir, "client.key") if args.cert_dir else None)

    ca_cert = _read_cert(ca_cert_path)
    client_cert = _read_cert(client_cert_path)
    client_key = _read_cert(client_key_path)

    if args.mqtt_test:
        result = asyncio.run(test_mqtt_connection(args.app_key, args.app_secret, args.base_url, args.system_id, args.mqtt_topic, ca_cert=ca_cert, client_cert=client_cert, client_key=client_key))
    else:
        action = "onboard" if args.onboard else ("offboard" if args.offboard else None)
        result = asyncio.run(test_sigenergy_api(args.app_key, args.app_secret, args.base_url, args.system_id, args.test_mode, action, mqtt_host=args.mqtt_host, ca_cert=ca_cert, client_cert=client_cert, client_key=client_key, readonly=args.readonly))
    raise SystemExit(result or 0)


if __name__ == "__main__":  # pragma: no cover
    main()
