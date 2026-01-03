# fmt: off
# pylint: disable=line-too-long
"""
Solis Cloud API integration for Predbat
Implements inverter control via Solis Cloud REST API using HMAC-SHA1 authentication
"""

import asyncio
import aiohttp
import argparse
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, UTC
from component_base import ComponentBase

# API Endpoints
SOLIS_BASE_URL = "https://www.soliscloud.com:13333"
SOLIS_READ_ENDPOINT = "/v2/api/atRead"
SOLIS_READ_BATCH_ENDPOINT = "/v2/api/atReadBatch"
SOLIS_CONTROL_ENDPOINT = "/v2/api/control"
SOLIS_INVERTER_LIST_ENDPOINT = "/v1/api/inverterList"
SOLIS_INVERTER_DETAIL_ENDPOINT = "/v1/api/inverterDetail"

# Retry configuration
SOLIS_MAX_RETRY_TIME = 30  # seconds
SOLIS_INITIAL_RETRY_DELAY = 1  # seconds
SOLIS_REQUEST_TIMEOUT = 30  # seconds

# CID Constants (Control IDs for inverter registers)
SOLIS_CID_STORAGE_MODE = 636
SOLIS_CID_BATTERY_RESERVE_SOC = 157
SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC = 158
SOLIS_CID_BATTERY_FORCE_CHARGE_SOC = 160
SOLIS_CID_BATTERY_RECOVERY_SOC = 7229
SOLIS_CID_BATTERY_MAX_CHARGE_SOC = 7963

# Power control CIDs
SOLIS_CID_POWER_LIMIT = 15
SOLIS_CID_MAX_OUTPUT_POWER = 376
SOLIS_CID_MAX_EXPORT_POWER = 499

# Allow export CID
SOLIS_CID_ALLOW_EXPORT = 6962

# Allow export values (inverted logic: "0" = allow, "1" = block)
SOLIS_ALLOW_EXPORT_ON = "0"  # Allow export
SOLIS_ALLOW_EXPORT_OFF = "1"  # Block export

# Storage mode bit positions for bitwise operations
SOLIS_BIT_BACKUP_MODE = 2  # Battery reserve/backup mode
SOLIS_BIT_GRID_CHARGING = 4  # Allow grid charging
SOLIS_BIT_TOU_MODE = 6  # Time of use mode

# Battery max current CIDs
SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT = 7224
SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT = 7226

# Charge slot CIDs (base + slot_index for slots 1-6)
SOLIS_CID_CHARGE_ENABLE_BASE = 5916  # 5916-5921
SOLIS_CID_CHARGE_SOC_BASE = 5928  # 5928-5933
# Non-sequential time CIDs for charge slots 1-6
SOLIS_CID_CHARGE_TIME = [5946, 5949, 5952, 5955, 5958, 5961]
# Non-sequential current CIDs for charge slots 1-6
SOLIS_CID_CHARGE_CURRENT = [5948, 5951, 5954, 5957, 5960, 5963]

# Discharge slot CIDs (base + slot_index for slots 1-6)
SOLIS_CID_DISCHARGE_ENABLE_BASE = 5922  # 5922-5927
# Non-sequential time CIDs for discharge slots 1-6
SOLIS_CID_DISCHARGE_TIME = [5964, 5968, 5972, 5976, 5980, 5987]
# Non-sequential SOC CIDs for discharge slots 1-6
SOLIS_CID_DISCHARGE_SOC = [5965, 5969, 5973, 5977, 5981, 5984]
# Non-sequential current CIDs for discharge slots 1-6
SOLIS_CID_DISCHARGE_CURRENT = [5967, 5971, 5975, 5979, 5983, 5986]

# Live data CIDs (frequent poll - every 5 minutes)
SOLIS_CID_BATTERY_SOC = 103
SOLIS_CID_BATTERY_POWER = 104
SOLIS_CID_GRID_POWER = 110
SOLIS_CID_BATTERY_TEMP = 115
SOLIS_CID_INVERTER_STATUS = 116
SOLIS_CID_BATTERY_VOLTAGE = 117
SOLIS_CID_BATTERY_CURRENT = 118

# Frequent poll CIDs (live data - every 5 minutes)
SOLIS_CID_FREQUENT = [
    SOLIS_CID_BATTERY_SOC,
    SOLIS_CID_BATTERY_POWER,
    SOLIS_CID_GRID_POWER,
    SOLIS_CID_BATTERY_TEMP,
    SOLIS_CID_INVERTER_STATUS,
    SOLIS_CID_BATTERY_VOLTAGE,
    SOLIS_CID_BATTERY_CURRENT,
]

# Infrequent poll CIDs (settings - every 60 minutes)
SOLIS_CID_INFREQUENT = [
    # Allow export
    SOLIS_CID_ALLOW_EXPORT,
    # Charge slot 1-6 enables
    *range(SOLIS_CID_CHARGE_ENABLE_BASE, SOLIS_CID_CHARGE_ENABLE_BASE + 6),
    # Discharge slot 1-6 enables
    *range(SOLIS_CID_DISCHARGE_ENABLE_BASE, SOLIS_CID_DISCHARGE_ENABLE_BASE + 6),
    # Charge slot 1-6 SOCs
    *range(SOLIS_CID_CHARGE_SOC_BASE, SOLIS_CID_CHARGE_SOC_BASE + 6),
    # Charge slot 1-6 times (non-sequential!)
    *SOLIS_CID_CHARGE_TIME,
    # Charge slot 1-6 currents (non-sequential!)
    *SOLIS_CID_CHARGE_CURRENT,
    # Discharge slot 1-6 times (non-sequential!)
    *SOLIS_CID_DISCHARGE_TIME,
    # Discharge slot 1-6 SOCs (non-sequential!)
    *SOLIS_CID_DISCHARGE_SOC,
    # Discharge slot 1-6 currents (non-sequential!)
    *SOLIS_CID_DISCHARGE_CURRENT,
    # Storage mode
    SOLIS_CID_STORAGE_MODE,
    # Battery limits
    SOLIS_CID_BATTERY_RESERVE_SOC,
    SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC,
    SOLIS_CID_BATTERY_FORCE_CHARGE_SOC,
    SOLIS_CID_BATTERY_RECOVERY_SOC,
    SOLIS_CID_BATTERY_MAX_CHARGE_SOC,
    # Battery max currents
    SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT,
    SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT,
    # Power controls
    SOLIS_CID_POWER_LIMIT,
    SOLIS_CID_MAX_OUTPUT_POWER,
    SOLIS_CID_MAX_EXPORT_POWER,
]

# CID metadata mapping
SOLIS_CID_MAP = {
    # Live data
    SOLIS_CID_BATTERY_SOC: {"name": "battery_soc", "unit": "%", "device_class": "battery", "state_class": "measurement"},
    SOLIS_CID_BATTERY_POWER: {"name": "battery_power", "unit": "W", "device_class": "power", "state_class": "measurement"},
    SOLIS_CID_GRID_POWER: {"name": "grid_power", "unit": "W", "device_class": "power", "state_class": "measurement"},
    SOLIS_CID_BATTERY_TEMP: {"name": "battery_temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement"},
    SOLIS_CID_INVERTER_STATUS: {"name": "inverter_status", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_BATTERY_VOLTAGE: {"name": "battery_voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement"},
    SOLIS_CID_BATTERY_CURRENT: {"name": "battery_current", "unit": "A", "device_class": "current", "state_class": "measurement"},
    
    # Charge slot 1-6 enables
    SOLIS_CID_CHARGE_ENABLE_BASE: {"name": "charge_slot1_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_ENABLE_BASE+1: {"name": "charge_slot2_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_ENABLE_BASE+2: {"name": "charge_slot3_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_ENABLE_BASE+3: {"name": "charge_slot4_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_ENABLE_BASE+4: {"name": "charge_slot5_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_ENABLE_BASE+5: {"name": "charge_slot6_enable", "unit": None, "device_class": None, "state_class": None},
    
    # Discharge slot 1-6 enables
    SOLIS_CID_DISCHARGE_ENABLE_BASE: {"name": "discharge_slot1_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_DISCHARGE_ENABLE_BASE+1: {"name": "discharge_slot2_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_DISCHARGE_ENABLE_BASE+2: {"name": "discharge_slot3_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_DISCHARGE_ENABLE_BASE+3: {"name": "discharge_slot4_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_DISCHARGE_ENABLE_BASE+4: {"name": "discharge_slot5_enable", "unit": None, "device_class": None, "state_class": None},
    SOLIS_CID_DISCHARGE_ENABLE_BASE+5: {"name": "discharge_slot6_enable", "unit": None, "device_class": None, "state_class": None},
    
    # Charge slot 1-6 SOCs
    SOLIS_CID_CHARGE_SOC_BASE: {"name": "charge_slot1_soc", "unit": "%", "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_SOC_BASE+1: {"name": "charge_slot2_soc", "unit": "%", "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_SOC_BASE+2: {"name": "charge_slot3_soc", "unit": "%", "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_SOC_BASE+3: {"name": "charge_slot4_soc", "unit": "%", "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_SOC_BASE+4: {"name": "charge_slot5_soc", "unit": "%", "device_class": None, "state_class": None},
    SOLIS_CID_CHARGE_SOC_BASE+5: {"name": "charge_slot6_soc", "unit": "%", "device_class": None, "state_class": None},
    
    # Charge slot 1-6 times (correct non-sequential CIDs)
    5946: {"name": "charge_slot1_time", "unit": None, "device_class": None, "state_class": None},
    5949: {"name": "charge_slot2_time", "unit": None, "device_class": None, "state_class": None},
    5952: {"name": "charge_slot3_time", "unit": None, "device_class": None, "state_class": None},
    5955: {"name": "charge_slot4_time", "unit": None, "device_class": None, "state_class": None},
    5958: {"name": "charge_slot5_time", "unit": None, "device_class": None, "state_class": None},
    5961: {"name": "charge_slot6_time", "unit": None, "device_class": None, "state_class": None},
    
    # Charge slot 1-6 currents (correct non-sequential CIDs)
    5948: {"name": "charge_slot1_current", "unit": "A", "device_class": "current", "state_class": None},
    5951: {"name": "charge_slot2_current", "unit": "A", "device_class": "current", "state_class": None},
    5954: {"name": "charge_slot3_current", "unit": "A", "device_class": "current", "state_class": None},
    5957: {"name": "charge_slot4_current", "unit": "A", "device_class": "current", "state_class": None},
    5960: {"name": "charge_slot5_current", "unit": "A", "device_class": "current", "state_class": None},
    5963: {"name": "charge_slot6_current", "unit": "A", "device_class": "current", "state_class": None},
    
    # Discharge slot 1-6 times (correct non-sequential CIDs)
    5964: {"name": "discharge_slot1_time", "unit": None, "device_class": None, "state_class": None},
    5968: {"name": "discharge_slot2_time", "unit": None, "device_class": None, "state_class": None},
    5972: {"name": "discharge_slot3_time", "unit": None, "device_class": None, "state_class": None},
    5976: {"name": "discharge_slot4_time", "unit": None, "device_class": None, "state_class": None},
    5980: {"name": "discharge_slot5_time", "unit": None, "device_class": None, "state_class": None},
    5987: {"name": "discharge_slot6_time", "unit": None, "device_class": None, "state_class": None},
    
    # Discharge slot 1-6 SOCs (correct non-sequential CIDs)
    5965: {"name": "discharge_slot1_soc", "unit": "%", "device_class": None, "state_class": None},
    5969: {"name": "discharge_slot2_soc", "unit": "%", "device_class": None, "state_class": None},
    5973: {"name": "discharge_slot3_soc", "unit": "%", "device_class": None, "state_class": None},
    5977: {"name": "discharge_slot4_soc", "unit": "%", "device_class": None, "state_class": None},
    5981: {"name": "discharge_slot5_soc", "unit": "%", "device_class": None, "state_class": None},
    5984: {"name": "discharge_slot6_soc", "unit": "%", "device_class": None, "state_class": None},
    
    # Discharge slot 1-6 currents (correct non-sequential CIDs)
    5967: {"name": "discharge_slot1_current", "unit": "A", "device_class": "current", "state_class": None},
    5971: {"name": "discharge_slot2_current", "unit": "A", "device_class": "current", "state_class": None},
    5975: {"name": "discharge_slot3_current", "unit": "A", "device_class": "current", "state_class": None},
    5979: {"name": "discharge_slot4_current", "unit": "A", "device_class": "current", "state_class": None},
    5983: {"name": "discharge_slot5_current", "unit": "A", "device_class": "current", "state_class": None},
    5986: {"name": "discharge_slot6_current", "unit": "A", "device_class": "current", "state_class": None},
    
    # Storage mode and battery limits
    636: {"name": "storage_mode", "unit": None, "device_class": None, "state_class": None},
    157: {"name": "reserve_soc", "unit": "%", "device_class": "battery", "state_class": None},
    158: {"name": "over_discharge_soc", "unit": "%", "device_class": "battery", "state_class": None},
    160: {"name": "force_charge_soc", "unit": "%", "device_class": "battery", "state_class": None},
    7229: {"name": "recovery_soc", "unit": "%", "device_class": "battery", "state_class": None},
    7963: {"name": "max_charge_soc", "unit": "%", "device_class": "battery", "state_class": None},
    
    # Battery max currents
    7224: {"name": "max_charge_current", "unit": "A", "device_class": "current", "state_class": None},
    7226: {"name": "max_discharge_current", "unit": "A", "device_class": "current", "state_class": None},
    
    # Power controls
    15: {"name": "power_limit", "unit": "%", "device_class": None, "state_class": None},
    376: {"name": "max_output_power", "unit": "%", "device_class": None, "state_class": None},
    499: {"name": "max_export_power", "unit": "W", "device_class": "power", "state_class": None},
}

# Storage mode mappings
SOLIS_STORAGE_MODES = {
    "Self-Use - No Grid Charging": 1,
    "Timed Charge/Discharge - No Grid Charging": 3,
    "Backup/Reserve - No Grid Charging": 17,
    "Self-Use - No Timed Charge/Discharge": 33,
    "Self-Use": 35,
    "Off-Grid Mode": 37,
    "Battery Awaken": 41,
    "Battery Awaken + Timed Charge/Discharge": 43,
    "Backup/Reserve - No Timed Charge/Discharge": 49,
    "Backup/Reserve": 51,
    "Feed-in priority - No Grid Charging": 64,
    "Feed-in priority - No Timed Charge/Discharge": 96,
    "Feed-in priority": 98,
}

# Inverter status codes
SOLIS_INVERTER_STATUS = {
    0: "Offline",
    1: "Standby",
    2: "Starting",
    3: "Running",
    4: "Fault",
    5: "Permanent Fault",
}

# API response codes
SOLIS_API_CODES = {
    "0": "Success",
    "10001": "Operation failed",
    "10400": "Request not authenticated",
    "10401": "Unauthorized",
    "10403": "Forbidden",
    "10404": "Not found",
    "10500": "Internal server error",
}

# Time options for selectors (HH:MM:SS format)
BASE_TIME = datetime(2000, 1, 1, 0, 0, 0)
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M:%S")) for minute in range(0, 24 * 60, 1)]


class SolisAPIError(Exception):
    """Custom exception for Solis API errors"""
    def __init__(self, message, status_code=None, response_code=None):
        self.status_code = status_code
        self.response_code = response_code
        final_message = message
        if status_code is not None:
            final_message = f"{final_message} (HTTP status code: {status_code})"
        if response_code is not None:
            final_message = f"{final_message} (API response code: {response_code})"
        super().__init__(final_message)


class SolisAPI(ComponentBase):
    """Solis Cloud API integration component"""
    
    def initialize(self, api_key, api_secret, inverter_sn=None, automatic=False, base_url=SOLIS_BASE_URL):
        """Initialize the Solis API component"""
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.automatic = automatic
        self.session = None
        self.nominal_voltage = 48  # Default nominal battery voltage
        
        # Convert inverter_sn to list
        if inverter_sn is None:
            self.inverter_sn = []
        elif isinstance(inverter_sn, str):
            self.inverter_sn = [inverter_sn]
        else:
            self.inverter_sn = inverter_sn
        
        # Cache structures
        self.cached_values = {}  # {inverter_sn: {cid: str_value}}
        self.inverter_details = {}  # {inverter_sn: detail_dict}
        self.storage_modes = {}  # {inverter_sn: {name: value}}
        self.parallel_battery_count = {}  # {inverter_sn: int}
        self.max_charge_current = {}  # {inverter_sn: int}
        self.max_discharge_current = {}  # {inverter_sn: int}
        
        # Tracking
        self.slots_reset = set()  # Track which inverters had slots reset
        
        self.log(f"Solis API: Initialized with inverter_sn={self.inverter_sn} automatic={automatic}")
    
    # ==================== Authentication Methods ====================
    
    def _digest(self, body):
        """Generate MD5 digest of request body, base64 encoded"""
        return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")
    
    def _sign_authorization(self, message):
        """Generate HMAC-SHA1 signature, base64 encoded"""
        hmac_object = hmac.new(
            self.api_secret.encode("utf-8"),
            msg=message.encode("utf-8"),
            digestmod=hashlib.sha1,
        )
        return base64.b64encode(hmac_object.digest()).decode("utf-8")
    
    def _format_date(self, dt=None):
        """Format datetime to GMT string"""
        if dt is None:
            dt = datetime.now(UTC)
        return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    def _build_headers(self, endpoint, payload):
        """Build HTTP headers with HMAC-SHA1 authorization"""
        body = json.dumps(payload)
        payload_digest = self._digest(body)
        content_type = "application/json"
        date_formatted = self._format_date()
        
        # Authorization string to sign (newline-separated)
        authorization_str = "\n".join([
            "POST",
            payload_digest,
            content_type,
            date_formatted,
            endpoint
        ])
        
        authorization_sign = self._sign_authorization(authorization_str)
        authorization = f"API {self.api_key}:{authorization_sign}"
        
        return {
            "Content-MD5": payload_digest,
            "Content-Type": content_type,
            "Date": date_formatted,
            "Authorization": authorization,
        }
    
    # ==================== Core API Methods ====================
    
    async def _execute_request(self, endpoint, payload):
        """Execute HTTP POST request to Solis API"""
        url = f"{self.base_url}{endpoint}"
        headers = self._build_headers(endpoint, payload)
        
        try:
            async with asyncio.timeout(SOLIS_REQUEST_TIMEOUT):
                async with self.session.post(url, headers=headers, json=payload) as response:
                    # Check HTTP status
                    if response.status != 200:
                        error_text = await response.text()
                        raise SolisAPIError(f"HTTP error: {error_text}", status_code=response.status)
                    
                    # Parse JSON response
                    response_json = await response.json()
                    
                    # Check API response code
                    code = response_json.get("code", "Unknown")
                    if str(code) != "0":
                        error_msg = response_json.get("msg", "Unknown error")
                        error_detail = SOLIS_API_CODES.get(str(code), f"Unknown code: {code}")
                        raise SolisAPIError(f"API error: {error_msg} ({error_detail})", response_code=str(code))
                    
                    # Return data field
                    return response_json.get("data")
        
        except asyncio.TimeoutError as err:
            raise SolisAPIError(f"Timeout accessing {url}") from err
        except aiohttp.ClientError as err:
            raise SolisAPIError(f"Network error accessing {url}: {str(err)}") from err
    
    async def _with_retry(self, operation, max_retry_time=SOLIS_MAX_RETRY_TIME):
        """Execute operation with exponential backoff retry"""
        start_time = time.monotonic()
        attempt = 0
        delay = SOLIS_INITIAL_RETRY_DELAY
        
        while True:
            try:
                return await operation()
            except SolisAPIError as err:
                elapsed_time = time.monotonic() - start_time
                if elapsed_time >= max_retry_time:
                    raise err
                
                attempt += 1
                self.log(f"Warn: Solis API retry {attempt} after {elapsed_time:.1f}s: {str(err)}")
                
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_retry_time - elapsed_time)  # Exponential backoff
    
    async def read_cid(self, inverter_sn, cid):
        """Read single CID value"""
        async def read_operation():
            payload = {"inverterSn": inverter_sn, "cid": cid}
            data = await self._execute_request(SOLIS_READ_ENDPOINT, payload)
            if data is None:
                raise SolisAPIError(f"Read CID {cid} failed: missing 'data' field")
            if "msg" not in data:
                raise SolisAPIError(f"Read CID {cid} failed: missing 'msg' field")
            return data["msg"]
        
        return await self._with_retry(read_operation)
    
    async def read_batch(self, inverter_sn, cids):
        """Read multiple CID values in batch"""
        async def read_batch_operation():
            # Convert CID list to comma-separated string
            cids_str = ",".join(str(cid) for cid in cids)
            payload = {"inverterSn": inverter_sn, "cids": cids_str}
            data = await self._execute_request(SOLIS_READ_BATCH_ENDPOINT, payload)
            
            if data is None:
                raise SolisAPIError("Batch read failed: missing 'data' field")
            
            # Parse nested response arrays: [[{"cid": "123", "msg": "value"}]]
            result = {}
            if isinstance(data, list):
                for outer_item in data:
                    if isinstance(outer_item, list):
                        for item in outer_item:
                            if "cid" in item and "msg" in item:
                                result[int(item["cid"])] = item["msg"]
            
            return result
        
        return await self._with_retry(read_batch_operation)
    
    async def write_cid(self, inverter_sn, cid, value, old_value=None, field_description=None):
        """Write CID value with optional old value verification.
        Automatically updates cache on success and logs appropriate messages.
        
        Args:
            inverter_sn: Inverter serial number
            cid: CID to write
            value: Value to write
            old_value: Previous value for verification (optional)
            field_description: Human-readable description for logging (e.g., "charge slot 1 SOC")
        
        Returns: True on success, False on failure
        """
        async def write_operation():
            payload = {
                "inverterSn": inverter_sn,
                "cid": cid,
                "value": str(value)
            }
            if old_value is not None:
                payload["yuanzhi"] = str(old_value)
            
            data = await self._execute_request(SOLIS_CONTROL_ENDPOINT, payload)
            
            if data is None:
                raise SolisAPIError(f"Write CID {cid} failed: missing 'data' field")
            if not isinstance(data, list):
                raise SolisAPIError(f"Write CID {cid} failed: 'data' field is not an array")
            
            # Validate response codes
            for item in data:
                code = item.get("code")
                if code is not None and str(code) != "0":
                    error_msg = item.get("msg", "Unknown error")
                    raise SolisAPIError(f"Write CID {cid} failed: {error_msg}", response_code=str(code))
        
        try:
            await self._with_retry(write_operation)
            
            # Update cache on success
            if inverter_sn not in self.cached_values:
                self.cached_values[inverter_sn] = {}
            self.cached_values[inverter_sn][cid] = str(value)
            
            # Log success
            if field_description:
                self.log(f"Solis API: Set {field_description} on {inverter_sn}")
            else:
                self.log(f"Solis API: Set CID {cid} to {value} on {inverter_sn}")
            
            return True
        except Exception as e:
            # Log failure
            if field_description:
                self.log(f"Warn: Solis API: Failed to set {field_description} on {inverter_sn}: {e}")
            else:
                self.log(f"Warn: Solis API write_cid failed for CID {cid}: {e}")
            return False
    
    async def get_inverter_list(self):
        """Get list of all inverters in account"""
        async def list_operation():
            payload = {"pageSize": "100"}
            data = await self._execute_request(SOLIS_INVERTER_LIST_ENDPOINT, payload)
            print(data)
            
            if data is None or "page" not in data:
                raise SolisAPIError("Inverter list failed: missing 'data.page' field")
            
            records = data["page"].get("records", [])
            return records
        
        return await self._with_retry(list_operation)
    
    async def get_inverter_detail(self, inverter_sn):
        """Get detailed information for specific inverter"""
        async def detail_operation():
            payload = {"sn": inverter_sn}
            data = await self._execute_request(SOLIS_INVERTER_DETAIL_ENDPOINT, payload)
            
            if data is None:
                raise SolisAPIError(f"Inverter detail failed for {inverter_sn}: missing 'data' field")
            
            return data
        
        return await self._with_retry(detail_operation)
    
    # ==================== Configuration and Polling ====================
    
    async def automatic_config(self):
        """Automatically configure Predbat base args based on discovered inverters"""
        if not self.inverter_sn:
            self.log("Warn: Solis API automatic_config: No inverters to configure")
            return
        
        num_inverters = len(self.inverter_sn)
        self.log(f"Solis API: Configuring Predbat for {num_inverters} inverter(s)")
        
        # Convert SNs to lowercase for entity naming
        devices = [sn.lower() for sn in self.inverter_sn]
        
        # Configure base Predbat settings
        self.set_arg("inverter_type", ["SolisCloud" for _ in range(num_inverters)])
        self.set_arg("num_inverters", num_inverters)
        
        # Battery and inverter entities
        self.set_arg("soc_percent", [f"sensor.predbat_solis_{device}_battery_soc" for device in devices])
        self.set_arg("battery_power", [f"sensor.predbat_solis_{device}_battery_power" for device in devices])
        self.set_arg("grid_power", [f"sensor.predbat_solis_{device}_grid_power" for device in devices])
        self.set_arg("battery_voltage", [f"sensor.predbat_solis_{device}_battery_voltage" for device in devices])
        self.set_arg("battery_temperature", [f"sensor.predbat_solis_{device}_battery_temperature" for device in devices])
        
        # Battery capacity and limits from cached details
        self.set_arg("soc_max", [f"sensor.predbat_solis_{device}_battery_capacity_soc" for device in devices])
        
        # Reserve and limits
        self.set_arg("reserve", [f"sensor.predbat_solis_{device}_reserve_soc" for device in devices])
        self.set_arg("battery_min_soc", [f"sensor.predbat_solis_{device}_over_discharge_soc" for device in devices])
        
        # Charge/discharge controls - using slot 1 for Predbat primary control
        self.set_arg("charge_start_time", [f"select.predbat_solis_{device}_charge_slot1_time" for device in devices])
        self.set_arg("charge_end_time", [f"select.predbat_solis_{device}_charge_slot1_time" for device in devices])  # Same selector, parsed
        self.set_arg("charge_limit", [f"sensor.predbat_solis_{device}_charge_slot1_soc" for device in devices])
        self.set_arg("charge_rate", [f"sensor.predbat_solis_{device}_charge_slot1_power" for device in devices])
        self.set_arg("scheduled_charge_enable", [f"sensor.predbat_solis_{device}_charge_slot1_enable" for device in devices])
        
        self.set_arg("discharge_start_time", [f"select.predbat_solis_{device}_discharge_slot1_time" for device in devices])
        self.set_arg("discharge_end_time", [f"select.predbat_solis_{device}_discharge_slot1_time" for device in devices])
        self.set_arg("discharge_target_soc", [f"sensor.predbat_solis_{device}_discharge_slot1_soc" for device in devices])
        self.set_arg("discharge_rate", [f"sensor.predbat_solis_{device}_discharge_slot1_power" for device in devices])
        self.set_arg("scheduled_discharge_enable", [f"sensor.predbat_solis_{device}_discharge_slot1_enable" for device in devices])
        self.set_arg("battery_rate_max", [f"sensor.predbat_solis_{device}_max_charge_power" for device in devices])
        
        self.log("Solis API: Automatic configuration complete")
    
    async def poll_inverter_data(self, inverter_sn, cid_list):
        """Poll CID values for specific inverter"""
        try:
            values = await self.read_batch(inverter_sn, cid_list)
            
            # Initialize cache for this inverter if not exists
            if inverter_sn not in self.cached_values:
                self.cached_values[inverter_sn] = {}
            
            # Update cached values
            self.cached_values[inverter_sn].update(values)
            
            return True
        
        except Exception as e:
            self.log(f"Warn: Solis API poll failed for {inverter_sn}: {e}")
            # Preserve old cached values
            return False
    
    def _calculate_max_currents(self, inverter_sn):
        """Calculate maximum charge/discharge currents for inverter"""
        values = self.cached_values.get(inverter_sn, {})
        battery_count = self.parallel_battery_count.get(inverter_sn, 1)
        
        # Calculate max charge current
        max_charge = 100  # Default fallback
        max_charge_current_str = values.get(SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT)
        voltage = values.get(SOLIS_CID_BATTERY_VOLTAGE)
        if max_charge_current_str:
            try:
                per_battery_max = float(max_charge_current_str)
                max_charge = int(per_battery_max * battery_count)
            except (ValueError, TypeError):
                pass
        self.max_charge_current[inverter_sn] = max_charge
        
        # Calculate max discharge current
        max_discharge = 100  # Default fallback
        max_discharge_current_str = values.get(SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT)
        if max_discharge_current_str:
            try:
                per_battery_max = float(max_discharge_current_str)
                max_discharge = int(per_battery_max * battery_count)
            except (ValueError, TypeError):
                pass
        self.max_discharge_current[inverter_sn] = max_discharge
        self.max_discharge_rate[inverter_sn] = max_discharge * self.nominal_voltage # Approximate power in W
        
        self.log(f"Solis API: Calculated max currents for {inverter_sn}: charge={max_charge}A, discharge={max_discharge}A")
    
    async def reset_unused_slots(self, inverter_sn):
        """Reset unused charge/discharge slots (one-time on startup)"""
        try:
            self.log(f"Solis API: Resetting unused slots for {inverter_sn}...")
            
            # Disable charge slots 2-6 (enable CIDs)
            for slot in range(2, 7):
                cid = SOLIS_CID_CHARGE_ENABLE_BASE + (slot - 1)
                await self.write_cid(inverter_sn, cid, "0")
            
            # Disable discharge slots 2-6 (enable CIDs)
            for slot in range(2, 7):
                cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + (slot - 1)
                await self.write_cid(inverter_sn, cid, "0")
            
            # Set max charge SOC to 100%
            await self.write_cid(inverter_sn, SOLIS_CID_BATTERY_MAX_CHARGE_SOC, "100", field_description="max charge SOC to 100%")
            
            self.log(f"Solis API: Successfully reset slots for {inverter_sn}")
        
        except Exception as e:
            self.log(f"Error: Solis API slot reset failed for {inverter_sn}: {e}")
    
    async def publish_entities(self):
        """Publish all entities to Home Assistant"""
        prefix = self.prefix
        
        for inverter_sn in self.inverter_sn:
            # Get inverter details for friendly name
            detail = self.inverter_details.get(inverter_sn, {})
            inverter_name = detail.get("inverterName", inverter_sn)
            
            # Get cached values for this inverter
            values = self.cached_values.get(inverter_sn, {})
            
            # Publish live data sensors
            for cid, value_str in values.items():
                if cid not in SOLIS_CID_MAP:
                    continue
                
                metadata = SOLIS_CID_MAP[cid]
                field_name = metadata["name"]
                entity_id = f"sensor.{prefix}_solis_{inverter_sn}_{field_name}"
                
                # Convert value if numeric
                value = value_str
                if value is not None:
                    try:
                        if metadata["unit"] in ["%", "W", "A", "V", "°C"]:
                            value = float(value_str)
                    except (ValueError, TypeError):
                        pass
                
                # Build attributes
                attributes = {
                    "friendly_name": f"Solis {inverter_name} {field_name.replace('_', ' ').title()}",
                }
                if metadata["unit"]:
                    attributes["unit_of_measurement"] = metadata["unit"]
                if metadata["device_class"]:
                    attributes["device_class"] = metadata["device_class"]
                if metadata["state_class"]:
                    attributes["state_class"] = metadata["state_class"]
                
                self.dashboard_item(entity_id, state=value, attributes=attributes, app="solis")
            
            # Publish sensors from inverter detail API (not CID-based)
            # Total Load Energy
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_total_load_energy"
            total_load = detail.get("homeLoadTotalEnergy")
            self.dashboard_item(
                entity_id,
                state=total_load,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Total Load Energy",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:home-lightning-bolt",
                },
                app="solis"
            )
            
            # Total Export Energy
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_total_export_energy"
            total_export = detail.get("gridSellTotalEnergy")
            self.dashboard_item(
                entity_id,
                state=total_export,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Total Export Energy",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:transmission-tower-export",
                },
                app="solis"
            )
            
            # Total Import Energy
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_total_import_energy"
            total_import = detail.get("gridPurchasedTotalEnergy")
            self.dashboard_item(
                entity_id,
                state=total_import,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Total Import Energy",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:transmission-tower-import",
                },
                app="solis"
            )
            
            # Battery Capacity SOC (from detail API, not live CID)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_battery_capacity_soc"
            capacity_soc = detail.get("batteryCapacitySoc")
            self.dashboard_item(
                entity_id,
                state=capacity_soc,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Capacity SOC",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "icon": "mdi:battery-50",
                },
                app="solis"
            )
            
            # PV Energy Total
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_pv_energy_total"
            pv_total = detail.get("eTotal")
            pv_total_unit = detail.get("eTotalStr", "kWh")
            self.dashboard_item(
                entity_id,
                state=pv_total,
                attributes={
                    "friendly_name": f"Solis {inverter_name} PV Energy Total",
                    "unit_of_measurement": pv_total_unit,
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:solar-power",
                },
                app="solis"
            )
            
            # PV Power (Real-time AC output power)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_pv_power_ac"
            pv_power = detail.get("pac")
            pv_power_unit = detail.get("pacStr", "kW")
            self.dashboard_item(
                entity_id,
                state=pv_power,
                attributes={
                    "friendly_name": f"Solis {inverter_name} PV Power",
                    "unit_of_measurement": pv_power_unit,
                    "device_class": "power",
                    "state_class": "measurement",
                    "icon": "mdi:solar-power",
                },
                app="solis"
            )
            
            # Publish charge slot controls (all 6 slots)
            for slot_num in range(1, 7):
                # Enable switch
                entity_id = f"switch.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_enable"
                enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE + (slot_num - 1)
                state = values.get(enable_cid, None)
                self.dashboard_item(
                    entity_id,
                    state=state,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Charge Slot {slot_num} Enable",
                        "icon": "mdi:battery-charging",
                    },
                    app="solis"
                )
                
                # Start time selector
                entity_id = f"select.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_start_time"
                time_cid = SOLIS_CID_CHARGE_TIME[slot_num - 1]
                time_value = values.get(time_cid, "00:00:00")
                self.dashboard_item(
                    entity_id,
                    state=time_value,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Charge Slot {slot_num} Start Time",
                        "options": OPTIONS_TIME,
                        "icon": "mdi:clock-start",
                    },
                    app="solis"
                )
                
                # End time selector (derived from time slot "HH:MM-HH:MM")
                entity_id = f"select.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_end_time"
                self.dashboard_item(
                    entity_id,
                    state="00:00:00",
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Charge Slot {slot_num} End Time",
                        "options": OPTIONS_TIME,
                        "icon": "mdi:clock-end",
                    },
                    app="solis"
                )
                
                # SOC target number
                entity_id = f"number.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_soc"
                soc_cid = SOLIS_CID_CHARGE_SOC_BASE + (slot_num - 1)
                soc_value = values.get(soc_cid, None)
                self.dashboard_item(
                    entity_id,
                    state=soc_value,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Charge Slot {slot_num} SOC",
                        "unit_of_measurement": "%",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "icon": "mdi:battery",
                    },
                    app="solis"
                )
                
                # Current limit number (displayed as power in watts)
                entity_id = f"number.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_power"
                current_cid = SOLIS_CID_CHARGE_CURRENT[slot_num - 1]
                current_value_amps = values.get(current_cid, None)
                
                # Convert amps to watts for display
                current_value_watts = None
                if current_value_amps is not None:
                    try:
                        current_value_watts = int(float(current_value_amps) * self.nominal_voltage)
                    except (ValueError, TypeError):
                        pass
                
                # Use pre-calculated max current (convert to watts)
                max_current_amps = self.max_charge_current.get(inverter_sn, 100)
                max_power_watts = int(max_current_amps * self.nominal_voltage)
                
                self.dashboard_item(
                    entity_id,
                    state=current_value_watts,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Charge Slot {slot_num} Power",
                        "unit_of_measurement": "W",
                        "min": 0,
                        "max": max_power_watts,
                        "step": self.nominal_voltage,
                        "device_class": "power",
                        "icon": "mdi:flash",
                    },
                    app="solis"
                )
            
            # Publish discharge slot controls (all 6 slots)
            for slot_num in range(1, 7):
                # Enable switch
                entity_id = f"switch.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_enable"
                enable_cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + (slot_num - 1)
                state = values.get(enable_cid, None)
                self.dashboard_item(
                    entity_id,
                    state=state,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Discharge Slot {slot_num} Enable",
                        "icon": "mdi:battery-minus",
                    },
                    app="solis"
                )
                
                # Start time selector
                entity_id = f"select.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_start_time"
                time_cid = SOLIS_CID_DISCHARGE_TIME[slot_num - 1]
                time_value = values.get(time_cid, "00:00:00")
                self.dashboard_item(
                    entity_id,
                    state=time_value,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Discharge Slot {slot_num} Start Time",
                        "options": OPTIONS_TIME,
                        "icon": "mdi:clock-start",
                    },
                    app="solis"
                )
                
                # End time selector
                entity_id = f"select.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_end_time"
                self.dashboard_item(
                    entity_id,
                    state="00:00:00",
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Discharge Slot {slot_num} End Time",
                        "options": OPTIONS_TIME,
                        "icon": "mdi:clock-end",
                    },
                    app="solis"
                )
                
                # SOC target number
                entity_id = f"number.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_soc"
                soc_cid = SOLIS_CID_DISCHARGE_SOC[slot_num - 1]
                soc_value = values.get(soc_cid, None)
                self.dashboard_item(
                    entity_id,
                    state=soc_value,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Discharge Slot {slot_num} SOC",
                        "unit_of_measurement": "%",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "icon": "mdi:battery",
                    },
                    app="solis"
                )
                
                # Current limit number (displayed as power in watts)
                entity_id = f"number.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_power"
                current_cid = SOLIS_CID_DISCHARGE_CURRENT[slot_num - 1]
                current_value_amps = values.get(current_cid, None)
                
                # Convert amps to watts for display
                current_value_watts = None
                if current_value_amps is not None:
                    try:
                        current_value_watts = int(float(current_value_amps) * self.nominal_voltage)
                    except (ValueError, TypeError):
                        pass
                
                # Use pre-calculated max current (convert to watts)
                max_current_amps = self.max_discharge_current.get(inverter_sn, 100)
                max_power_watts = int(max_current_amps * self.nominal_voltage)
                
                self.dashboard_item(
                    entity_id,
                    state=current_value_watts,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} Discharge Slot {slot_num} Power",
                        "unit_of_measurement": "W",
                        "min": 0,
                        "max": max_power_watts,
                        "step": self.nominal_voltage,
                        "device_class": "power",
                        "icon": "mdi:flash",
                    },
                    app="solis"
                )
            
            # Storage mode selector
            entity_id = f"select.{prefix}_solis_{inverter_sn}_storage_mode"
            mode_value = values.get(SOLIS_CID_STORAGE_MODE, None)
            # Use dynamic storage modes if available, otherwise static
            mode_options = list(self.storage_modes.get(inverter_sn, SOLIS_STORAGE_MODES).keys())
            self.dashboard_item(
                entity_id,
                state=mode_value,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Storage Mode",
                    "options": mode_options,
                    "icon": "mdi:battery-sync",
                },
                app="solis"
            )
            
            # Storage mode bit switches
            storage_mode_int = None
            if mode_value is not None:
                try:
                    storage_mode_int = int(mode_value)
                except (ValueError, TypeError):
                    pass
            
            # Battery reserve switch
            entity_id = f"switch.{prefix}_solis_{inverter_sn}_battery_reserve"
            reserve_on = (storage_mode_int & (1 << SOLIS_BIT_BACKUP_MODE)) != 0 if storage_mode_int is not None else None
            self.dashboard_item(
                entity_id,
                state="1" if reserve_on else "0" if reserve_on is not None else None,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Reserve",
                    "icon": "mdi:battery-heart-outline",
                },
                app="solis"
            )
            
            # Allow grid charging switch
            entity_id = f"switch.{prefix}_solis_{inverter_sn}_allow_grid_charging"
            grid_charging_on = (storage_mode_int & (1 << SOLIS_BIT_GRID_CHARGING)) != 0 if storage_mode_int is not None else None
            self.dashboard_item(
                entity_id,
                state="1" if grid_charging_on else "0" if grid_charging_on is not None else None,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Allow Grid Charging",
                    "icon": "mdi:battery-charging-outline",
                },
                app="solis"
            )
            
            # Time of use switch
            entity_id = f"switch.{prefix}_solis_{inverter_sn}_time_of_use"
            tou_on = (storage_mode_int & (1 << SOLIS_BIT_TOU_MODE)) != 0 if storage_mode_int is not None else None
            self.dashboard_item(
                entity_id,
                state="1" if tou_on else "0" if tou_on is not None else None,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Time of Use Mode",
                    "icon": "mdi:clock-check-outline",
                },
                app="solis"
            )
            
            # Allow export switch (inverted logic: "0" = allow, "1" = block)
            entity_id = f"switch.{prefix}_solis_{inverter_sn}_allow_export"
            allow_export_value = values.get(SOLIS_CID_ALLOW_EXPORT, None)
            allow_export_on = (allow_export_value == SOLIS_ALLOW_EXPORT_ON) if allow_export_value is not None else None
            self.dashboard_item(
                entity_id,
                state="1" if allow_export_on else "0" if allow_export_on is not None else None,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Allow Export",
                    "icon": "mdi:transmission-tower-export",
                },
                app="solis"
            )
            
            # Battery SOC limit numbers
            for cid, name, friendly, icon in [
                (SOLIS_CID_BATTERY_RESERVE_SOC, "reserve_soc", "Battery Reserve SOC", "mdi:battery-50"),
                (SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC, "over_discharge_soc", "Battery Over Discharge SOC", "mdi:battery-50"),
                (SOLIS_CID_BATTERY_FORCE_CHARGE_SOC, "force_charge_soc", "Battery Force Charge SOC", "mdi:battery-alert"),
                (SOLIS_CID_BATTERY_RECOVERY_SOC, "recovery_soc", "Battery Recovery SOC", "mdi:battery-50"),
                (SOLIS_CID_BATTERY_MAX_CHARGE_SOC, "max_charge_soc", "Battery Max Charge SOC", "mdi:battery"),
            ]:
                entity_id = f"number.{prefix}_solis_{inverter_sn}_{name}"
                soc_value = values.get(cid, None)
                self.dashboard_item(
                    entity_id,
                    state=soc_value,
                    attributes={
                        "friendly_name": f"Solis {inverter_name} {friendly}",
                        "unit_of_measurement": "%",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "device_class": "battery",
                        "icon": icon,
                    },
                    app="solis"
                )
            
            # Battery max current numbers (displayed as power in watts)
            entity_id = f"number.{prefix}_solis_{inverter_sn}_max_charge_power"
            max_charge_current_amps = values.get(SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT, None)
            
            # Convert amps to watts for display
            max_charge_power_watts = None
            if max_charge_current_amps is not None:
                try:
                    max_charge_power_watts = int(float(max_charge_current_amps) * self.nominal_voltage)
                except (ValueError, TypeError):
                    pass
            
            self.dashboard_item(
                entity_id,
                state=max_charge_power_watts,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Max Charge Power",
                    "unit_of_measurement": "W",
                    "min": 0,
                    "max": int(1000 * self.nominal_voltage),
                    "step": self.nominal_voltage,
                    "device_class": "power",
                    "icon": "mdi:battery-arrow-down-outline",
                },
                app="solis"
            )
            
            entity_id = f"number.{prefix}_solis_{inverter_sn}_max_discharge_power"
            max_discharge_current_amps = values.get(SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT, None)
            
            # Convert amps to watts for display
            max_discharge_power_watts = None
            if max_discharge_current_amps is not None:
                try:
                    max_discharge_power_watts = int(float(max_discharge_current_amps) * self.nominal_voltage)
                except (ValueError, TypeError):
                    pass
            
            self.dashboard_item(
                entity_id,
                state=max_discharge_power_watts,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Max Discharge Power",
                    "unit_of_measurement": "W",
                    "min": 0,
                    "max": int(1000 * self.nominal_voltage),
                    "step": self.nominal_voltage,
                    "device_class": "power",
                    "icon": "mdi:battery-arrow-up-outline",
                },
                app="solis"
            )
            
            # Power control numbers
            entity_id = f"number.{prefix}_solis_{inverter_sn}_power_limit"
            power_limit_value = values.get(SOLIS_CID_POWER_LIMIT, None)
            self.dashboard_item(
                entity_id,
                state=power_limit_value,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Power Limit",
                    "unit_of_measurement": "%",
                    "min": 0,
                    "max": 110,
                    "step": 1,
                    "icon": "mdi:transmission-tower-export",
                },
                app="solis"
            )
            
            entity_id = f"number.{prefix}_solis_{inverter_sn}_max_output_power"
            max_output_power_value = values.get(SOLIS_CID_MAX_OUTPUT_POWER, None)
            self.dashboard_item(
                entity_id,
                state=max_output_power_value,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Max Output Power",
                    "unit_of_measurement": "%",
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "icon": "mdi:lightning-bolt-outline",
                },
                app="solis"
            )
            
            entity_id = f"number.{prefix}_solis_{inverter_sn}_max_export_power"
            max_export_power_value = values.get(SOLIS_CID_MAX_EXPORT_POWER, None)
            self.dashboard_item(
                entity_id,
                state=max_export_power_value,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Max Export Power",
                    "unit_of_measurement": "W",
                    "min": 0,
                    "max": 1000000,
                    "step": 100,
                    "device_class": "power",
                    "icon": "mdi:transmission-tower-export",
                },
                app="solis"
            )
    
    # ==================== Control Methods ====================
    
    async def set_charge_schedule(self, inverter_sn, slots):
        """Set charge schedule (only slot 1 is actively used)"""
        try:
            for slot_data in slots:
                slot_num = slot_data.get("slot", 1)
                if slot_num != 1:
                    continue  # Only program slot 1
                
                # Convert HH:MM:SS to HH:MM-HH:MM format
                start_time = slot_data.get("start_time", "00:00:00")
                end_time = slot_data.get("end_time", "00:00:00")
                start_hhmm = start_time[:5]  # Strip :SS
                end_hhmm = end_time[:5]
                time_str = f"{start_hhmm}-{end_hhmm}"
                
                enable = "1" if slot_data.get("enable", True) else "0"
                soc = str(slot_data.get("soc", 100))
                current = str(slot_data.get("current", 50))
                
                # Write CIDs with delay between writes
                enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE + (slot_num - 1)
                success = await self.write_cid(inverter_sn, enable_cid, enable)  # Enable
                
                time_cid = SOLIS_CID_CHARGE_TIME[slot_num - 1]
                success |= await self.write_cid(inverter_sn, time_cid, time_str)  # Time
                
                soc_cid = SOLIS_CID_CHARGE_SOC_BASE + (slot_num - 1)
                success |= await self.write_cid(inverter_sn, soc_cid, soc)  # SOC
                
                current_cid = SOLIS_CID_CHARGE_CURRENT[slot_num - 1]
                success |= await self.write_cid(inverter_sn, current_cid, current)  # Current
                
                if not success:
                    self.log(f"Warn: Solis API set charge schedule encountered errors for {inverter_sn} slot {slot_num}")
        
        except Exception as e:
            self.log(f"Warn: Solis API set charge schedule failed for {inverter_sn}: {e}")
    
    async def set_discharge_schedule(self, inverter_sn, slots):
        """Set discharge schedule (only slot 1 is actively used)"""
        try:
            for slot_data in slots:
                slot_num = slot_data.get("slot", 1)
                if slot_num != 1:
                    continue  # Only program slot 1
                
                # Convert HH:MM:SS to HH:MM-HH:MM format
                start_time = slot_data.get("start_time", "00:00:00")
                end_time = slot_data.get("end_time", "00:00:00")
                start_hhmm = start_time[:5]  # Strip :SS
                end_hhmm = end_time[:5]
                time_str = f"{start_hhmm}-{end_hhmm}"
                
                enable = "1" if slot_data.get("enable", True) else "0"
                soc = str(slot_data.get("soc", 10))
                current = str(slot_data.get("current", 50))
                
                # Write CIDs with delay between writes
                enable_cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + (slot_num - 1)
                success = await self.write_cid(inverter_sn, enable_cid, enable)  # Enable
                
                time_cid = SOLIS_CID_DISCHARGE_TIME[slot_num - 1]
                success |= await self.write_cid(inverter_sn, time_cid, time_str)  # Time
                
                soc_cid = SOLIS_CID_DISCHARGE_SOC[slot_num - 1]
                success |= await self.write_cid(inverter_sn, soc_cid, soc)  # SOC
                
                current_cid = SOLIS_CID_DISCHARGE_CURRENT[slot_num - 1]
                success |= await self.write_cid(inverter_sn, current_cid, current)  # Current                

                if not success:
                    self.log(f"Warn: Solis API set discharge schedule encountered errors for {inverter_sn} slot {slot_num}")
        
        except Exception as e:
            self.log(f"Warn: Solis API set discharge schedule failed for {inverter_sn}: {e}")
    
    async def set_storage_mode(self, inverter_sn, mode):
        """Set storage mode"""
        try:
            # Convert mode name to value
            modes = self.storage_modes.get(inverter_sn, SOLIS_STORAGE_MODES)
            mode_value = modes.get(mode)
            if mode_value is None:
                self.log(f"Error: Unknown storage mode '{mode}' for {inverter_sn}")
                return
            
            # Write storage mode CID
            success = await self.write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, mode_value)
            if not success:
                self.log(f"Warn: Solis API set storage mode encountered errors for {inverter_sn}")
        
        except Exception as e:
            self.log(f"Warn: Solis API set storage mode failed for {inverter_sn}: {e}")
    
    # ==================== Event Handlers ====================
    
    def _calculate_toggle_value(self, service, current_value):
        """Calculate new value based on service and current value"""
        if service == "turn_on":
            return "1"
        elif service == "turn_off":
            return "0"
        elif service == "toggle":
            return "0" if current_value == "1" else "1"
        else:
            return None
    
    async def select_event(self, entity_id, value):
        """Handle select entity changes"""
        try:
            # Parse entity_id: select.{prefix}_solis_{sn}_{field}
            # Example: select.predbat_solis_123456_charge_slot1_start_time
            prefix = self.base.get_arg("prefix", "predbat")
            entity_prefix = f"select.{prefix}_solis_"
            
            if not entity_id.startswith(entity_prefix):
                return
            
            remainder = entity_id[len(entity_prefix):]
            parts = remainder.split("_")
            
            if len(parts) < 2:
                return
            
            inverter_sn = parts[0]
            field = "_".join(parts[1:])
            
            # Validate inverter exists
            if inverter_sn not in self.inverter_sn:
                self.log(f"Warn: Solis API: Unknown inverter {inverter_sn} in select_event")
                return
            
            # Handle storage mode
            if field == "storage_mode":
                await self.set_storage_mode(inverter_sn, value)
                
                # Update cache if write succeeded
                try:
                    read_value = await self.read_cid(inverter_sn, SOLIS_CID_STORAGE_MODE)
                    if inverter_sn not in self.cached_values:
                        self.cached_values[inverter_sn] = {}
                    self.cached_values[inverter_sn][SOLIS_CID_STORAGE_MODE] = read_value
                    self.log(f"Solis API: Updated cache for storage mode on {inverter_sn}")
                except Exception as e:
                    self.log(f"Warn: Solis API: Failed to update cache for storage mode: {e}")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle charge slot times
            if field.startswith("charge_slot") and ("start_time" in field or "end_time" in field):
                # Extract slot number
                slot_match = field.replace("charge_slot", "").split("_")[0]
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    return
                
                # Get time CID for this slot
                time_cid = SOLIS_CID_CHARGE_TIME[slot_num - 1]
                
                # Get current cached time value (format: "HH:MM-HH:MM")
                if inverter_sn not in self.cached_values:
                    self.cached_values[inverter_sn] = {}
                
                current_time = self.cached_values[inverter_sn].get(time_cid, "00:00-00:00")
                
                # Parse existing start and end
                if "-" in current_time:
                    start_hhmm, end_hhmm = current_time.split("-", 1)
                else:
                    start_hhmm, end_hhmm = "00:00", "00:00"
                
                # Update the relevant time component (strip :SS from HH:MM:SS)
                new_hhmm = value[:5] if len(value) >= 5 else value
                
                if "start_time" in field:
                    start_hhmm = new_hhmm
                elif "end_time" in field:
                    end_hhmm = new_hhmm
                
                # Combine into API format
                time_str = f"{start_hhmm}-{end_hhmm}"
                
                # Write to inverter
                await self.write_cid(inverter_sn, time_cid, time_str, field_description=f"charge slot {slot_num} time to {time_str}")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle discharge slot times
            if field.startswith("discharge_slot") and ("start_time" in field or "end_time" in field):
                # Extract slot number
                slot_match = field.replace("discharge_slot", "").split("_")[0]
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    return
                
                # Get time CID for this slot
                time_cid = SOLIS_CID_DISCHARGE_TIME[slot_num - 1]
                
                # Get current cached time value (format: "HH:MM-HH:MM")
                if inverter_sn not in self.cached_values:
                    self.cached_values[inverter_sn] = {}
                
                current_time = self.cached_values[inverter_sn].get(time_cid, "00:00-00:00")
                
                # Parse existing start and end
                if "-" in current_time:
                    start_hhmm, end_hhmm = current_time.split("-", 1)
                else:
                    start_hhmm, end_hhmm = "00:00", "00:00"
                
                # Update the relevant time component (strip :SS from HH:MM:SS)
                new_hhmm = value[:5] if len(value) >= 5 else value
                
                if "start_time" in field:
                    start_hhmm = new_hhmm
                elif "end_time" in field:
                    end_hhmm = new_hhmm
                
                # Combine into API format
                time_str = f"{start_hhmm}-{end_hhmm}"
                
                # Write to inverter
                await self.write_cid(inverter_sn, time_cid, time_str, field_description=f"discharge slot {slot_num} time to {time_str}")
                
                # Re-publish entities
                await self.publish_entities()
                return
        
        except Exception as e:
            self.log(f"Error: Solis API select_event failed for {entity_id}: {e}")
    
    async def number_event(self, entity_id, value):
        """Handle number entity changes"""
        try:
            # Parse entity_id: number.{prefix}_solis_{sn}_{field}
            # Example: number.predbat_solis_123456_charge_slot1_soc
            prefix = self.base.get_arg("prefix", "predbat")
            entity_prefix = f"number.{prefix}_solis_"
            
            if not entity_id.startswith(entity_prefix):
                return
            
            remainder = entity_id[len(entity_prefix):]
            parts = remainder.split("_")
            
            if len(parts) < 2:
                return
            
            inverter_sn = parts[0]
            field = "_".join(parts[1:])
            
            # Validate inverter exists
            if inverter_sn not in self.inverter_sn:
                self.log(f"Warn: Solis API: Unknown inverter {inverter_sn} in number_event")
                return
            
            # Convert value to string for API
            value_str = str(int(value))
            
            # Handle charge slot SOC
            if field.startswith("charge_slot") and field.endswith("_soc"):
                # Extract slot number
                slot_match = field.replace("charge_slot", "").replace("_soc", "")
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    return
                
                # Get SOC CID for this slot
                soc_cid = SOLIS_CID_CHARGE_SOC_BASE + (slot_num - 1)
                
                # Write to inverter
                await self.write_cid(inverter_sn, soc_cid, value_str, field_description=f"charge slot {slot_num} SOC to {value_str}%")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle charge slot power (user provides watts, convert to amps)
            if field.startswith("charge_slot") and field.endswith("_power"):
                # Extract slot number
                slot_match = field.replace("charge_slot", "").replace("_power", "")
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    return
                
                # Get current CID for this slot
                current_cid = SOLIS_CID_CHARGE_CURRENT[slot_num - 1]
                
                # Convert watts to amps for inverter
                amps = int(value / self.nominal_voltage)
                amps_str = str(amps)
                
                # Write to inverter
                await self.write_cid(inverter_sn, current_cid, amps_str, field_description=f"charge slot {slot_num} power to {value_str}W ({amps}A)")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle discharge slot SOC
            if field.startswith("discharge_slot") and field.endswith("_soc"):
                # Extract slot number
                slot_match = field.replace("discharge_slot", "").replace("_soc", "")
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    return
                
                # Get SOC CID for this slot
                soc_cid = SOLIS_CID_DISCHARGE_SOC[slot_num - 1]
                
                # Write to inverter
                await self.write_cid(inverter_sn, soc_cid, value_str, field_description=f"discharge slot {slot_num} SOC to {value_str}%")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle discharge slot power (user provides watts, convert to amps)
            if field.startswith("discharge_slot") and field.endswith("_power"):
                # Extract slot number
                slot_match = field.replace("discharge_slot", "").replace("_power", "")
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    return
                
                # Get current CID for this slot
                current_cid = SOLIS_CID_DISCHARGE_CURRENT[slot_num - 1]
                
                # Convert watts to amps for inverter
                amps = int(value / self.nominal_voltage)
                amps_str = str(amps)
                
                # Write to inverter
                await self.write_cid(inverter_sn, current_cid, amps_str, field_description=f"discharge slot {slot_num} power to {value_str}W ({amps}A)")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle battery SOC limits
            if field in ["reserve_soc", "over_discharge_soc", "force_charge_soc", "recovery_soc", "max_charge_soc"]:
                cid_map = {
                    "reserve_soc": SOLIS_CID_BATTERY_RESERVE_SOC,
                    "over_discharge_soc": SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC,
                    "force_charge_soc": SOLIS_CID_BATTERY_FORCE_CHARGE_SOC,
                    "recovery_soc": SOLIS_CID_BATTERY_RECOVERY_SOC,
                    "max_charge_soc": SOLIS_CID_BATTERY_MAX_CHARGE_SOC,
                }
                cid = cid_map[field]
                
                # Write to inverter
                await self.write_cid(inverter_sn, cid, value_str, field_description=f"{field} to {value_str}%")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle battery max power (user provides watts, convert to amps)
            if field in ["max_charge_power", "max_discharge_power"]:
                cid_map = {
                    "max_charge_power": SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT,
                    "max_discharge_power": SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT,
                }
                cid = cid_map[field]
                
                # Convert watts to amps for inverter
                amps = int(value / self.nominal_voltage)
                amps_str = str(amps)
                
                # Write to inverter
                await self.write_cid(inverter_sn, cid, amps_str, field_description=f"{field} to {value_str}W ({amps}A)")
                
                # Re-calculate max currents after change
                self._calculate_max_currents(inverter_sn)
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle power controls
            if field in ["power_limit", "max_output_power", "max_export_power"]:
                cid_map = {
                    "power_limit": SOLIS_CID_POWER_LIMIT,
                    "max_output_power": SOLIS_CID_MAX_OUTPUT_POWER,
                    "max_export_power": SOLIS_CID_MAX_EXPORT_POWER,
                }
                cid = cid_map[field]
                
                # Write to inverter
                await self.write_cid(inverter_sn, cid, value_str, field_description=f"{field} to {value_str}")
                
                # Re-publish entities
                await self.publish_entities()
                return
        
        except Exception as e:
            self.log(f"Error: Solis API number_event failed for {entity_id}: {e}")
    
    async def switch_event(self, entity_id, service):
        """Handle switch entity changes"""
        try:
            # Parse entity_id: switch.{prefix}_solis_{sn}_{field}
            # Example: switch.predbat_solis_123456_charge_slot1_enable
            prefix = self.base.get_arg("prefix", "predbat")
            entity_prefix = f"switch.{prefix}_solis_"
            
            if not entity_id.startswith(entity_prefix):
                return
            
            remainder = entity_id[len(entity_prefix):]
            parts = remainder.split("_")
            
            if len(parts) < 2:
                return
            
            inverter_sn = parts[0]
            field = "_".join(parts[1:])
            
            # Validate inverter exists
            if inverter_sn not in self.inverter_sn:
                self.log(f"Warn: Solis API: Unknown inverter {inverter_sn} in switch_event")
                return
            
            # Handle charge slot enables
            if field.startswith("charge_slot") and field.endswith("_enable"):
                # Extract slot number
                slot_match = field.replace("charge_slot", "").replace("_enable", "")
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    self.log(f"Warn: Solis API: Slot number {slot_num} out of range in {field}")
                    return
                
                # Get enable CID for this slot
                enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE + (slot_num - 1)
                
                # Determine value from service
                current_value = self.cached_values.get(inverter_sn, {}).get(enable_cid, "0")
                value = self._calculate_toggle_value(service, current_value)
                if value is None:
                    self.log(f"Warn: Solis API: Unknown service '{service}' for {entity_id}")
                    return
                
                # Write to inverter
                await self.write_cid(inverter_sn, enable_cid, value, field_description=f"charge slot {slot_num} enable to {value}")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle discharge slot enables
            if field.startswith("discharge_slot") and field.endswith("_enable"):
                # Extract slot number
                slot_match = field.replace("discharge_slot", "").replace("_enable", "")
                try:
                    slot_num = int(slot_match)
                except (ValueError, IndexError):
                    self.log(f"Warn: Solis API: Cannot parse slot number from {field}")
                    return
                
                if slot_num < 1 or slot_num > 6:
                    self.log("Warn: Solis API: Slot number out of range in switch_event")
                    return
                
                # Get enable CID for this slot
                enable_cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + (slot_num - 1)
                
                # Determine value from service
                current_value = self.cached_values.get(inverter_sn, {}).get(enable_cid, "0")
                value = self._calculate_toggle_value(service, current_value)
                if value is None:
                    self.log(f"Warn: Solis API: Unknown service '{service}' for {entity_id}")
                    return
                
                # Write to inverter
                await self.write_cid(inverter_sn, enable_cid, value, field_description=f"discharge slot {slot_num} enable to {value}")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle battery reserve switch
            if field == "battery_reserve":
                # Get current storage mode
                if inverter_sn not in self.cached_values:
                    self.cached_values[inverter_sn] = {}
                
                current_mode_str = self.cached_values[inverter_sn].get(SOLIS_CID_STORAGE_MODE, "0")
                try:
                    current_mode = int(current_mode_str)
                except (ValueError, TypeError):
                    self.log(f"Warn: Solis API: Invalid storage mode value: {current_mode_str}")
                    return
                
                # Calculate new value
                if service == "turn_on":
                    new_mode = current_mode | (1 << SOLIS_BIT_BACKUP_MODE)
                elif service == "turn_off":
                    new_mode = current_mode & ~(1 << SOLIS_BIT_BACKUP_MODE)
                elif service == "toggle":
                    new_mode = current_mode ^ (1 << SOLIS_BIT_BACKUP_MODE)
                else:
                    self.log(f"Warn: Solis API: Unknown service '{service}' for switch")
                    return
                
                # Write to inverter
                await self.write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, str(new_mode), field_description=f"battery reserve to {service} (mode: {current_mode} -> {new_mode})")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle allow grid charging switch
            if field == "allow_grid_charging":
                # Get current storage mode
                if inverter_sn not in self.cached_values:
                    self.cached_values[inverter_sn] = {}
                
                current_mode_str = self.cached_values[inverter_sn].get(SOLIS_CID_STORAGE_MODE, "0")
                try:
                    current_mode = int(current_mode_str)
                except (ValueError, TypeError):
                    self.log(f"Warn: Solis API: Invalid storage mode value: {current_mode_str}")
                    return
                
                # Calculate new value
                if service == "turn_on":
                    new_mode = current_mode | (1 << SOLIS_BIT_GRID_CHARGING)
                elif service == "turn_off":
                    new_mode = current_mode & ~(1 << SOLIS_BIT_GRID_CHARGING)
                elif service == "toggle":
                    new_mode = current_mode ^ (1 << SOLIS_BIT_GRID_CHARGING)
                else:
                    self.log(f"Warn: Solis API: Unknown service '{service}' for switch")
                    return
                
                # Write to inverter
                await self.write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, str(new_mode), field_description=f"allow grid charging to {service} (mode: {current_mode} -> {new_mode})")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle time of use switch
            if field == "time_of_use":
                # Get current storage mode
                if inverter_sn not in self.cached_values:
                    self.cached_values[inverter_sn] = {}
                
                current_mode_str = self.cached_values[inverter_sn].get(SOLIS_CID_STORAGE_MODE, "0")
                try:
                    current_mode = int(current_mode_str)
                except (ValueError, TypeError):
                    self.log(f"Warn: Solis API: Invalid storage mode value: {current_mode_str}")
                    return
                
                # Calculate new value
                if service == "turn_on":
                    new_mode = current_mode | (1 << SOLIS_BIT_TOU_MODE)
                elif service == "turn_off":
                    new_mode = current_mode & ~(1 << SOLIS_BIT_TOU_MODE)
                elif service == "toggle":
                    new_mode = current_mode ^ (1 << SOLIS_BIT_TOU_MODE)
                else:
                    self.log(f"Warn: Solis API: Unknown service '{service}' for switch")
                    return
                
                # Write to inverter
                await self.write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, str(new_mode), field_description=f"time of use to {service} (mode: {current_mode} -> {new_mode})")
                
                # Re-publish entities
                await self.publish_entities()
                return
            
            # Handle allow export switch (inverted logic)
            if field == "allow_export":
                # Get current value
                if inverter_sn not in self.cached_values:
                    self.cached_values[inverter_sn] = {}
                
                current_value = self.cached_values[inverter_sn].get(SOLIS_CID_ALLOW_EXPORT, SOLIS_ALLOW_EXPORT_OFF)
                
                # Calculate new value (inverted logic)
                if service == "turn_on":
                    new_value = SOLIS_ALLOW_EXPORT_ON  # "0" = allow export
                elif service == "turn_off":
                    new_value = SOLIS_ALLOW_EXPORT_OFF  # "1" = block export
                elif service == "toggle":
                    new_value = SOLIS_ALLOW_EXPORT_OFF if current_value == SOLIS_ALLOW_EXPORT_ON else SOLIS_ALLOW_EXPORT_ON
                else:
                    self.log(f"Warn: Solis API: Unknown service '{service}' for switch")
                    return
                
                # Write to inverter
                await self.write_cid(inverter_sn, SOLIS_CID_ALLOW_EXPORT, new_value, field_description=f"allow export to {service} (value: {current_value} -> {new_value})")
                
                # Re-publish entities
                await self.publish_entities()
                return
        
        except Exception as e:
            self.log(f"Error: Solis API switch_event failed for {entity_id}: {e}")

    async def fetch_inverter_details(self, sn):
        try:
            detail = await self.get_inverter_detail(sn)
            self.inverter_details[sn] = detail
            self.log(f"Solis API: Loaded details for inverter {sn}")
            
            # Extract parallel battery count (format: "2.0" means 3 batteries total)
            parallel_battery = detail.get("parallelBattery", "0")
            try:
                self.parallel_battery_count[sn] = int(float(parallel_battery)) + 1
            except (ValueError, TypeError):
                self.parallel_battery_count[sn] = 1
            
            # Extract storage modes if available in detail response
            # TODO: Check if API provides storage mode options in detail
            self.storage_modes[sn] = SOLIS_STORAGE_MODES
            return True
        
        except Exception as e:
            self.log(f"Warn: Solis API failed to load details for {sn}: {e}") 
            return False    

    # ==================== Component Lifecycle ====================

    async def run(self, seconds, first):
        """Main run cycle called every 5 seconds"""
        
        # One-time startup configuration
        if first:
            # Create aiohttp session
            timeout = aiohttp.ClientTimeout(total=SOLIS_REQUEST_TIMEOUT)
            self.session = aiohttp.ClientSession(timeout=timeout)

            # Discover inverters - always scan, filter by inverter_sn if specified
            try:
                self.log("Solis API: Discovering inverters...")
                all_inverters = await self.get_inverter_list()
                
                if all_inverters:
                    # Filter by configured inverter_sn if specified, otherwise use all
                    if self.inverter_sn:
                        # inverter_sn was configured, filter to only those
                        filtered = [inv for inv in all_inverters if inv.get("sn") in self.inverter_sn]
                        self.log(f"Solis API: Filtered to {len(filtered)} of {len(all_inverters)} inverter(s) based on config")
                        self.inverter_sn = [inv.get("sn") for inv in filtered if inv.get("sn")]
                    else:
                        # No filter configured, use all discovered inverters
                        self.inverter_sn = [inv.get("sn") for inv in all_inverters if inv.get("sn")]
                        self.log(f"Solis API: Using all {len(self.inverter_sn)} discovered inverter(s)")
                    
                    if not self.inverter_sn:
                        self.log("Warn: Solis API: No inverters found after filtering")
                else:
                    self.log("Warn: Solis API: No inverters discovered")
            
            except Exception as e:
                self.log(f"Error: Solis API: Inverter discovery failed: {e}")
                        
            # Get inverter details for all inverters
            for sn in self.inverter_sn:
                await self.fetch_inverter_details(sn)
            
            # Reset unused slots once
            for sn in self.inverter_sn:
                await self.reset_unused_slots(sn)

            if self.inverter_sn:
                self.log(f"Solis API: Managing {len(self.inverter_sn)} inverter(s): {', '.join(self.inverter_sn)}")
                self.api_started = True
            else:
                self.log("Error: Solis API: No inverters to manage after discovery")
                self.api_started = False
                return  # Stop further processing if no inverters
        
        # Frequent polling (every 5 minutes)
        if first or (seconds % 300 == 0):
            poll_success = True
            for sn in self.inverter_sn:
                success = await self.poll_inverter_data(sn, SOLIS_CID_FREQUENT)
                if not success:
                    poll_success = False
                success =  await self.fetch_inverter_details(sn) # Get inverter details for all inverters
                if not success:
                    poll_success = False                
            
            # Only update last_updated_time if all polls succeeded
            if poll_success:
                self.last_updated_time = self.base.now_utc
        
        # Infrequent polling (every 60 minutes)
        if first or (seconds % 3600 == 0):
            for sn in self.inverter_sn:
                await self.poll_inverter_data(sn, SOLIS_CID_INFREQUENT)
                # Recalculate max currents after polling infrequent data
                self._calculate_max_currents(sn)
        
        # Publish entities after polling
        if first or (seconds % 300 == 0):
            await self.publish_entities()

        # Auto-configure Predbat if enabled
        if first and self.automatic and self.inverter_sn:
            await self.automatic_config()

    
    async def final(self):
        """Cleanup on shutdown"""
        if self.session:
            await self.session.close()
            self.session = None
        self.log("Solis API: Component stopped")


class MockBase:  # pragma: no cover
    """Mock base class for testing"""

    def __init__(self):
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if "options" in attributes:
                attributes["options"] = "..."
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None):
        return self.args.get(key, default)

    def set_arg(self, key, value):
        self.args[key] = value
        state = None
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and "." in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")


async def test_solis_api(key_id, secret):  # pragma: no cover
    """
    Run a test of Solis API
    """
    print(f"Testing Solis API with key_id: {key_id[:10]}...")

    # Create a mock base object
    mock_base = MockBase()

    # Create SolisAPI instance with correct parameter names
    arg_dict = {"api_key": key_id, "api_secret": secret, "automatic": True}
    solis_api = SolisAPI(mock_base, **arg_dict)

    # Call run() once
    print("Calling run() once...")
    await solis_api.run(seconds=0, first=True)
    print("Run completed successfully")
    await solis_api.final()


def main():  # pragma: no cover
    """
    Main function for command line execution
    """
    parser = argparse.ArgumentParser(description="Test Solis Cloud API")
    parser.add_argument("--key-id", required=True, help="Solis Cloud API Key ID")
    parser.add_argument("--secret", required=True, help="Solis Cloud API Secret")

    args = parser.parse_args()
    key_id = args.key_id
    secret = args.secret

    # Run the test
    asyncio.run(test_solis_api(key_id, secret))


if __name__ == "__main__":
    main()
