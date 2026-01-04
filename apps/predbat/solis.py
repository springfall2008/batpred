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
SOLIS_CID_BATTERY_CAPACITY = 172

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

# TOU mode CID
SOLIS_CID_TOU_V2_MODE = 6798

# Live data CIDs (frequent poll - every 5 minutes)
SOLIS_CID_CHARGE_DISCHARGE_SETTINGS = 103

SOLIS_CID_SINGLE = [
    SOLIS_CID_CHARGE_DISCHARGE_SETTINGS,
]

# Infrequent poll CIDs (settings - every 60 minutes)
SOLIS_CID_INFREQUENT = [
    # Allow export
    SOLIS_CID_ALLOW_EXPORT,
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
    SOLIS_CID_BATTERY_CAPACITY,
]

SOLIS_CID_LIST_TOU_V2 = [
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
]

# CID metadata mapping
SOLIS_CID_MAP = {    
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
"""
BIT Tag Number | Fault Status | Status Code
BIT00 | Self-Consumption Mode Switch | 0—Off | 1—On
BIT01 | Optimized Revenue Mode Switch | 0—Off | 1—On
BIT02 | Off-Grid Energy Storage Mode Switch | 0—Off | 1—On
BIT03 | Battery Wake-up Switch (1—Wake-up Enabled | 0—Wake-up Disabled) | 0—Off | 1—On
BIT04 | Backup Battery Mode Switch | 0—Off | 1—On
BIT05 | Allow/Disallow Battery Charging from Grid | 0—Disallowed | 1—Allowed
BIT06 | Grid Priority Mode Switch | 0—Off | 1—On
BIT07 | Nighttime Battery Over-Discharge Retention Enable Switch | 0—Off | 1—On
BIT08 | Battery Power Supply Dynamic Adjustment Enable Switch During Strong Charging | 0—Off | 1—On
BIT09 | Battery Current Correction Enable Switch | 0—Off | 1—On
BIT10 | Battery Treatment Mode | 0—Off 1—On
BIT11 |Peak-shaving mode switch 0—Off 1—On
"""

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
    
    def initialize(self, api_key, api_secret, inverter_sn=None, automatic=False, base_url=SOLIS_BASE_URL, control_enable=True):
        """Initialize the Solis API component"""
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.automatic = automatic
        self.session = None
        self.nominal_voltage = 48.4  # Default nominal battery voltage
        self.control_enable = control_enable
        
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
        self.charge_discharge_time_windows = {}  # {inverter_sn: time_window_dict}
        
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
                        raise SolisAPIError(f"API error: {error_msg} ({error_detail} - {response_json})", response_code=str(code))
                    
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
                delay = min(delay * 1.5, max_retry_time - elapsed_time)  # Exponential backoff
    
    async def read_cid(self, inverter_sn, cid):
        """Read single CID value"""
        async def read_operation():
            payload = {"inverterSn": inverter_sn, "cid": cid}
            data = await self._execute_request(SOLIS_READ_ENDPOINT, payload)
            self.log("Read Payload: {} - result {}".format(payload, data))  # Debug log for payload and result
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
            self.log("Batch Read Payload: " + str(payload))  # Debug log for payload
            data = await self._execute_request(SOLIS_READ_BATCH_ENDPOINT, payload)
            self.log("Batch Read Payload: {} - result {}".format(payload, data))  # Debug log for payload and result
            
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
    
    async def encode_time_windows(self, inverter_sn, time_windows):
        """
        Encode charge/discharge time windows into CID 103 format
        
        Accepts time_windows dict with structure {slot_num: {charge_current, discharge_current, charge_start_time, charge_end_time, discharge_start_time, discharge_end_time, field_length}}

        Uses field_length to decide on how to encode (type 1 or type 2)        
        Returns: encoded string for CID 103
        """
        if not time_windows:
            self.log(f"Warn: Solis API encode_time_windows: No time windows data provided for {inverter_sn}")
            return None
        
        # Determine format from first slot's field_length
        first_slot = time_windows.get(1, time_windows.get(min(time_windows.keys())))
        field_length = first_slot.get("field_length", 18)  # Default to variant 1
        
        fields = []
        
        if field_length == 18:
            # Variant 1: 6 fields per slot (charge_current, discharge_current, charge_start, charge_end, discharge_start, discharge_end)
            self.log(f"Solis API: Encoding time windows variant 1 (18 fields) for {inverter_sn}")
            for slot_index in range(1, 4):  # Slots 1-3
                slot_data = time_windows.get(slot_index, {})
                
                # Extract fields with defaults
                charge_current = str(int(slot_data.get("charge_current", 0)))
                discharge_current = str(int(slot_data.get("discharge_current", 0)))
                charge_start = slot_data.get("charge_start_time", "00:00")
                charge_end = slot_data.get("charge_end_time", "00:00")
                discharge_start = slot_data.get("discharge_start_time", "00:00")
                discharge_end = slot_data.get("discharge_end_time", "00:00")
                
                # Add 6 fields for this slot
                fields.extend([charge_current, discharge_current, charge_start, charge_end, discharge_start, discharge_end])
                
        elif field_length == 12:
            # Variant 2: 4 fields per slot (charge_current, discharge_current, charge_time_slot, discharge_time_slot)
            self.log(f"Solis API: Encoding time windows variant 2 (12 fields) for {inverter_sn}")
            for slot_index in range(1, 4):  # Slots 1-3
                slot_data = time_windows.get(slot_index, {})
                
                # Extract fields with defaults
                charge_current = str(int(slot_data.get("charge_current", 0)))
                discharge_current = str(int(slot_data.get("discharge_current", 0)))
                charge_start = slot_data.get("charge_start_time", "00:00")
                charge_end = slot_data.get("charge_end_time", "00:00")
                discharge_start = slot_data.get("discharge_start_time", "00:00")
                discharge_end = slot_data.get("discharge_end_time", "00:00")
                
                # Combine into time slot format
                charge_time_slot = f"{charge_start}-{charge_end}"
                discharge_time_slot = f"{discharge_start}-{discharge_end}"
                
                # Add 4 fields for this slot
                fields.extend([charge_current, discharge_current, charge_time_slot, discharge_time_slot])
        else:
            self.log(f"Warn: Solis API encode_time_windows: Unexpected field_length {field_length} for {inverter_sn}")
            return None
        
        # Join fields with commas
        encoded = ",".join(fields)
        self.log(f"Solis API: Encoded time windows for {inverter_sn}: {encoded}")
        
        return encoded

    async def decode_time_windows_v2(self, inverter_sn):
        # Reads the new split registers and puts them into the same format as decode_time_windows
        result = {}
        for slot_index in range(1, 7):  # Slots 1-6
            charge_current_cid = SOLIS_CID_CHARGE_CURRENT[slot_index - 1]
            discharge_current_cid = SOLIS_CID_DISCHARGE_CURRENT[slot_index - 1]
            charge_time_cid = SOLIS_CID_CHARGE_TIME[slot_index - 1]
            discharge_time_cid = SOLIS_CID_DISCHARGE_TIME[slot_index - 1]
            charge_soc_cid = SOLIS_CID_CHARGE_SOC_BASE + (slot_index - 1)
            discharge_soc_cid = SOLIS_CID_DISCHARGE_SOC[slot_index - 1]
            charge_enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE + (slot_index - 1)
            discharge_enable_cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + (slot_index - 1)
            
            charge_current = self.cached_values.get(inverter_sn, {}).get(charge_current_cid, "0")
            discharge_current = self.cached_values.get(inverter_sn, {}).get(discharge_current_cid, "0")
            charge_time = self.cached_values.get(inverter_sn, {}).get(charge_time_cid, "00:00-00:00")
            discharge_time = self.cached_values.get(inverter_sn, {}).get(discharge_time_cid, "00:00-00:00")
            charge_soc = self.cached_values.get(inverter_sn, {}).get(charge_soc_cid, "0")
            discharge_soc = self.cached_values.get(inverter_sn, {}).get(discharge_soc_cid, "0")
            charge_enable = self.cached_values.get(inverter_sn, {}).get(charge_enable_cid, "0")
            discharge_enable = self.cached_values.get(inverter_sn, {}).get(discharge_enable_cid, "0")
            
            # Split time slots into start and end times
            charge_parts = charge_time.split("-") if "-" in charge_time else ["00:00", "00:00"]
            discharge_parts = discharge_time.split("-") if "-" in discharge_time else ["00:00", "00:00"]
            
            charge_start = charge_parts[0] if len(charge_parts) > 0 else "00:00"
            charge_end = charge_parts[1] if len(charge_parts) > 1 else "00:00"
            discharge_start = discharge_parts[0] if len(discharge_parts) > 0 else "00:00"
            discharge_end = discharge_parts[1] if len(discharge_parts) > 1 else "00:00"
            
            result[slot_index] = {
                "charge_current": float(charge_current) if charge_current else 0.0,
                "discharge_current": float(discharge_current) if discharge_current else 0.0,
                "charge_start_time": charge_start,
                "charge_end_time": charge_end,
                "discharge_start_time": discharge_start,
                "discharge_end_time": discharge_end,
                "charge_soc": float(charge_soc) if charge_soc else 0.0,
                "discharge_soc": float(discharge_soc) if discharge_soc else 0.0,
                "charge_enable": int(charge_enable) if charge_enable else 0,
                "discharge_enable": int(discharge_enable) if discharge_enable else 0,
                "field_length": 0,  # Indicate v2 format
            }
        self.charge_discharge_time_windows[inverter_sn] = result
        self.log("Solis API: Decoded time windows v2 for {}: {}".format(inverter_sn, result))  # Debug log
        return result

    async def reset_charge_windows_if_needed(self, inverter_sn):
        """
        Predbat only uses 1 slot so disable the others to avoid conflicts.
        """
        slots_to_check = range(2, 7)
        
        
        for slot in slots_to_check:
            slot_data = self.charge_discharge_time_windows[inverter_sn].get(slot)
            if not slot_data:
                continue
            slot_data["charge_start_time"] = "00:00"
            slot_data["charge_end_time"] = "00:00"
            slot_data["discharge_start_time"] = "00:00"
            slot_data["discharge_end_time"] = "00:00"
            slot_data["charge_enable"] = 0
            slot_data["discharge_enable"] = 0

    async def write_time_windows_if_changed(self, inverter_sn):
        """Write charge/discharge time windows, SOC, and current to inverter, only if values changed from cache.
        Automatically handles V1 vs V2 modes and only writes registers that have changed.
        
        Args:
            inverter_sn: Inverter serial number
            slot_num: Specific slot to update (1-6), or None to update all slots
            
        Returns: True if write succeeded or no changes needed, False on error
        """
        try:
            if inverter_sn not in self.charge_discharge_time_windows:
                self.log(f"Warn: Solis API: No time windows cached for {inverter_sn}")
                return True
            
            if self.is_tou_v2_mode(inverter_sn):
                # V2 mode: check and write individual registers for changed values only
                slots_to_check = range(1, 7)
                success = True
                
                for slot in slots_to_check:
                    slot_data = self.charge_discharge_time_windows[inverter_sn].get(slot)
                    if not slot_data:
                        continue
                    
                    # Check and write charge enable if changed
                    if "charge_enable" in slot_data:
                        enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE + (slot - 1)
                        new_enable_str = str(int(slot_data['charge_enable']))
                        cached_enable = self.cached_values.get(inverter_sn, {}).get(enable_cid)
                        if cached_enable != new_enable_str:
                            result = await self.read_and_write_cid(inverter_sn, enable_cid, new_enable_str,
                                                                   field_description=f"charge slot {slot} enable")
                            success &= result
                    
                    # Check and write charge time if changed
                    if "charge_start_time" in slot_data and "charge_end_time" in slot_data:
                        time_cid = SOLIS_CID_CHARGE_TIME[slot - 1]
                        new_time_str = f"{slot_data['charge_start_time']}-{slot_data['charge_end_time']}"
                        cached_time = self.cached_values.get(inverter_sn, {}).get(time_cid)
                        if cached_time != new_time_str:
                            result = await self.read_and_write_cid(inverter_sn, time_cid, new_time_str, 
                                                                   field_description=f"charge slot {slot} time")
                            success &= result
                    
                    # Check and write charge SOC if changed
                    if "charge_soc" in slot_data:
                        soc_cid = SOLIS_CID_CHARGE_SOC_BASE + (slot - 1)
                        new_soc_str = str(int(slot_data['charge_soc']))
                        cached_soc = self.cached_values.get(inverter_sn, {}).get(soc_cid)
                        if cached_soc != new_soc_str:
                            result = await self.read_and_write_cid(inverter_sn, soc_cid, new_soc_str,
                                                                   field_description=f"charge slot {slot} SOC")
                            success &= result
                    
                    # Check and write charge current if changed
                    if "charge_current" in slot_data:
                        current_cid = SOLIS_CID_CHARGE_CURRENT[slot - 1]
                        new_current_str = str(int(slot_data['charge_current']))
                        cached_current = self.cached_values.get(inverter_sn, {}).get(current_cid)
                        if cached_current != new_current_str:
                            result = await self.read_and_write_cid(inverter_sn, current_cid, new_current_str,
                                                                   field_description=f"charge slot {slot} current")
                            success &= result
                    
                    # Check and write discharge enable if changed
                    if "discharge_enable" in slot_data:
                        enable_cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + (slot - 1)
                        new_enable_str = str(int(slot_data['discharge_enable']))
                        cached_enable = self.cached_values.get(inverter_sn, {}).get(enable_cid)
                        if cached_enable != new_enable_str:
                            result = await self.read_and_write_cid(inverter_sn, enable_cid, new_enable_str,
                                                                   field_description=f"discharge slot {slot} enable")
                            success &= result
                    
                    # Check and write discharge time if changed
                    if "discharge_start_time" in slot_data and "discharge_end_time" in slot_data:
                        time_cid = SOLIS_CID_DISCHARGE_TIME[slot - 1]
                        new_time_str = f"{slot_data['discharge_start_time']}-{slot_data['discharge_end_time']}"
                        cached_time = self.cached_values.get(inverter_sn, {}).get(time_cid)
                        if cached_time != new_time_str:
                            result = await self.read_and_write_cid(inverter_sn, time_cid, new_time_str,
                                                                   field_description=f"discharge slot {slot} time")
                            success &= result
                    
                    # Check and write discharge SOC if changed
                    if "discharge_soc" in slot_data:
                        soc_cid = SOLIS_CID_DISCHARGE_SOC[slot - 1]
                        new_soc_str = str(int(slot_data['discharge_soc']))
                        cached_soc = self.cached_values.get(inverter_sn, {}).get(soc_cid)
                        if cached_soc != new_soc_str:
                            result = await self.read_and_write_cid(inverter_sn, soc_cid, new_soc_str,
                                                                   field_description=f"discharge slot {slot} SOC")
                            success &= result
                    
                    # Check and write discharge current if changed
                    if "discharge_current" in slot_data:
                        current_cid = SOLIS_CID_DISCHARGE_CURRENT[slot - 1]
                        new_current_str = str(int(slot_data['discharge_current']))
                        cached_current = self.cached_values.get(inverter_sn, {}).get(current_cid)
                        if cached_current != new_current_str:
                            result = await self.read_and_write_cid(inverter_sn, current_cid, new_current_str,
                                                                   field_description=f"discharge slot {slot} current")
                            success &= result
                
                return success
            else:
                # V1 mode: Handle SOC values separately (global CIDs) and time/current via CID 103
                success = True
                slots_to_check = range(1, 7)
                
                # Check and write charge SOC to global CID if changed
                for slot in slots_to_check:
                    slot_data = self.charge_discharge_time_windows[inverter_sn].get(slot)
                    if slot_data and "charge_soc" in slot_data:
                        new_soc_str = str(int(slot_data['charge_soc']))
                        cached_soc = self.cached_values.get(inverter_sn, {}).get(SOLIS_CID_BATTERY_FORCE_CHARGE_SOC)
                        if cached_soc != new_soc_str:
                            result = await self.read_and_write_cid(inverter_sn, SOLIS_CID_BATTERY_FORCE_CHARGE_SOC, new_soc_str,
                                                                   field_description=f"max charge SOC")
                            success &= result
                        break  # V1 mode: SOC is global, only need to check once
                
                # Check and write discharge SOC to global CID if changed
                for slot in slots_to_check:
                    slot_data = self.charge_discharge_time_windows[inverter_sn].get(slot)
                    if slot_data and "discharge_soc" in slot_data:
                        new_soc_str = str(int(slot_data['discharge_soc']))
                        cached_soc = self.cached_values.get(inverter_sn, {}).get(SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC)
                        if cached_soc != new_soc_str:
                            result = await self.read_and_write_cid(inverter_sn, SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC, new_soc_str,
                                                                   field_description=f"over discharge SOC")
                            success &= result
                        break  # V1 mode: SOC is global, only need to check once
                
                # Update enable flags based on time equality for all slots
                for slot in range(1, 7):
                    if slot not in self.charge_discharge_time_windows[inverter_sn]:
                        continue
                    slot_data = self.charge_discharge_time_windows[inverter_sn][slot]
                    
                    # Update charge enable based on time equality
                    if "charge_start_time" in slot_data and "charge_end_time" in slot_data:
                        if slot_data["charge_start_time"] == slot_data["charge_end_time"]:
                            slot_data["charge_enable"] = 0
                        else:
                            slot_data["charge_enable"] = 1
                    
                    # Update discharge enable based on time equality
                    if "discharge_start_time" in slot_data and "discharge_end_time" in slot_data:
                        if slot_data["discharge_start_time"] == slot_data["discharge_end_time"]:
                            slot_data["discharge_enable"] = 0
                        else:
                            slot_data["discharge_enable"] = 1
                
                # Encode all slots and compare with cached value (for time/current/enable)
                encoded = await self.encode_time_windows(inverter_sn, self.charge_discharge_time_windows[inverter_sn])
                cached_encoded = self.cached_values.get(inverter_sn, {}).get(SOLIS_CID_CHARGE_DISCHARGE_SETTINGS)
                
                if cached_encoded != encoded:
                    result = await self.read_and_write_cid(inverter_sn, SOLIS_CID_CHARGE_DISCHARGE_SETTINGS, encoded,
                                                            field_description="charge/discharge time windows")
                    success &= result
                else:
                    self.log(f"Solis API: Time windows unchanged for {inverter_sn}, skipping CID 103 write")
                
                return success
        
        except Exception as e:
            self.log(f"Error: Solis API write_time_windows_if_changed failed for {inverter_sn}: {e}")
            return False

    async def decode_time_windows(self, inverter_sn):
        """
        Decode charge/discharge time windows from cached CID 103
        
        Two variants exist:
        - Variant 1 (18 fields): charge_current, discharge_current, charge_start, charge_end, 
                                 discharge_start, discharge_end (repeated for 3 slots)
        - Variant 2 (12 fields): charge_current, discharge_current, charge_time_slot, 
                                 discharge_time_slot (repeated for 3 slots)
        
        Example Variant 1: '62,62,00:00,05:30,00:00,00:00,0,0,00:00,00:00,00:00,00:00,0,0,00:00,00:00,00:00,00:00'
        Example Variant 2: '62,62,00:00-05:30,00:00-00:00,0,0,00:00-00:00,00:00-00:00,0,0,00:00-00:00,00:00-00:00'
        
        Returns: dict with structure {slot_num: {charge_current, discharge_current, charge_time, discharge_time}}
        """
        data = self.cached_values.get(inverter_sn, {}).get(SOLIS_CID_CHARGE_DISCHARGE_SETTINGS, None)
        if data is None:
            self.log(f"Warn: Solis API decode_time_windows: No data for inverter {inverter_sn}")
            return None
        
        # Split into fields
        fields = data.split(",")
        fields = [f.strip() for f in fields]  # Remove whitespace
        fields_length = len(fields)
        
        result = {}

        global_charge_soc = self.cached_values.get(inverter_sn, {}).get(SOLIS_CID_BATTERY_FORCE_CHARGE_SOC)
        global_discharge_soc = self.cached_values.get(inverter_sn, {}).get(SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC)
        self.log("Solis API: Decode time windows, global_charge_soc={}, global_discharge_soc={}".format(global_charge_soc, global_discharge_soc))  # Debug log
        try:
            global_charge_soc = float(global_charge_soc)
        except (ValueError, TypeError):
            global_charge_soc = 100.0
        try:
            global_discharge_soc = float(global_discharge_soc)
        except (ValueError, TypeError):
            global_discharge_soc = 10.0

        self.log("Solis API: Decode time windows, global_charge_soc={}, global_discharge_soc={}".format(global_charge_soc, global_discharge_soc))  # Debug log
        
        if fields_length == 18:
            # Variant 1: 6 fields per slot (charge_current, discharge_current, charge_start, charge_end, discharge_start, discharge_end)
            self.log(f"Solis API: Decoding time windows variant 1 (18 fields) for {inverter_sn}")
            for slot_index in range(1, 4):  # Slots 1-3
                base_idx = (slot_index - 1) * 6
                
                # Extract fields
                charge_current = fields[base_idx] if base_idx < len(fields) else "0"
                discharge_current = fields[base_idx + 1] if base_idx + 1 < len(fields) else "0"
                charge_start = fields[base_idx + 2] if base_idx + 2 < len(fields) else "00:00"
                charge_end = fields[base_idx + 3] if base_idx + 3 < len(fields) else "00:00"
                discharge_start = fields[base_idx + 4] if base_idx + 4 < len(fields) else "00:00"
                discharge_end = fields[base_idx + 5] if base_idx + 5 < len(fields) else "00:00"
                
                result[slot_index] = {
                    "charge_current": float(charge_current) if charge_current else 0.0,
                    "discharge_current": float(discharge_current) if discharge_current else 0.0,
                    "charge_start_time": charge_start,
                    "charge_end_time": charge_end,
                    "discharge_start_time": discharge_start,
                    "discharge_end_time": discharge_end,
                    "charge_enable": 1 if charge_start != charge_end else 0,
                    "discharge_enable": 1 if discharge_start != discharge_end else 0,
                    "charge_soc": global_charge_soc,
                    "discharge_soc": global_discharge_soc,
                    "field_length": fields_length,
                }
                
        elif fields_length == 12:
            # Variant 2: 4 fields per slot (charge_current, discharge_current, charge_time_slot, discharge_time_slot)
            self.log(f"Solis API: Decoding time windows variant 2 (12 fields) for {inverter_sn}")
            for slot_index in range(1, 4):  # Slots 1-3
                base_idx = (slot_index - 1) * 4
                
                # Extract fields
                charge_current = fields[base_idx] if base_idx < len(fields) else "0"
                discharge_current = fields[base_idx + 1] if base_idx + 1 < len(fields) else "0"
                charge_time_slot = fields[base_idx + 2] if base_idx + 2 < len(fields) else "00:00-00:00"
                discharge_time_slot = fields[base_idx + 3] if base_idx + 3 < len(fields) else "00:00-00:00"
                
                # Split time slots into start and end times
                charge_parts = charge_time_slot.split("-") if "-" in charge_time_slot else ["00:00", "00:00"]
                discharge_parts = discharge_time_slot.split("-") if "-" in discharge_time_slot else ["00:00", "00:00"]
                
                charge_start = charge_parts[0] if len(charge_parts) > 0 else "00:00"
                charge_end = charge_parts[1] if len(charge_parts) > 1 else "00:00"
                discharge_start = discharge_parts[0] if len(discharge_parts) > 0 else "00:00"
                discharge_end = discharge_parts[1] if len(discharge_parts) > 1 else "00:00"
                
                result[slot_index] = {
                    "charge_current": float(charge_current) if charge_current else 0.0,
                    "discharge_current": float(discharge_current) if discharge_current else 0.0,
                    "charge_start_time": charge_start,
                    "charge_end_time": charge_end,
                    "discharge_start_time": discharge_start,
                    "discharge_end_time": discharge_end,
                    "charge_enable": 1 if charge_start != charge_end else 0,
                    "discharge_enable": 1 if discharge_start != discharge_end else 0,
                    "charge_soc": global_charge_soc,
                    "discharge_soc": global_discharge_soc,
                    "field_length": fields_length,
                }
        else:
            self.log(f"Warn: Solis API decode_time_windows: Unexpected field count {fields_length} for {inverter_sn}")
            return None
        
        self.log(f"Solis API: Decoded time windows for {inverter_sn}: {result}")
        self.charge_discharge_time_windows[inverter_sn] = result
        return result

    async def read_and_write_cid(self, inverter_sn, cid, value, field_description=None):
        """Read CID value then write with verification (required by Solis API).
        Automatically reads current value, writes with old_value verification,
        updates cache on success, and logs appropriate messages.
        
        Args:
            inverter_sn: Inverter serial number
            cid: CID to write
            value: Value to write
            field_description: Human-readable description for logging (e.g., "charge slot 1 SOC")
        
        Returns: True on success, False on failure
        """
        try:
            # Read current value first (required by Solis API)
            old_value = await self.read_cid(inverter_sn, cid)
            # Write with old_value verification
            if old_value == value:
                # No change needed
                self.log(f"Solis API: CID {cid} {field_description} on {inverter_sn} already set to {value}")
                return True
            return await self.write_cid(inverter_sn, cid, value, old_value=old_value, field_description=field_description)
        except Exception as e:
            # Log failure
            if field_description:
                self.log(f"Warn: Solis API: Failed to read and set {field_description} on {inverter_sn}: {e}")
            else:
                self.log(f"Warn: Solis API read_and_write_cid failed for CID {cid}: {e}")
            return False
    
    async def write_cid(self, inverter_sn, cid, value, old_value=None, field_description=None):
        """Write CID value with optional old value verification.
        Automatically updates cache on success and logs appropriate messages.
        
        Note: Many Solis API commands require reading the current value first.
        Use read_and_write_cid() wrapper for those cases.
        
        Args:
            inverter_sn: Inverter serial number
            cid: CID to write
            value: Value to write
            old_value: Previous value for verification (optional, required for many CIDs)
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

            self.log("Write Payload: " + str(payload))  # Debug log for payload
            
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
        self.set_arg("load_power", [f"sensor.predbat_solis_{device}_load_power" for device in devices])
        self.set_arg("pv_power", [f"sensor.predbat_solis_{device}_pv_power" for device in devices])
        self.set_arg("battery_voltage", [f"sensor.predbat_solis_{device}_battery_voltage" for device in devices])
        #self.set_arg("battery_temperature", [f"sensor.predbat_solis_{device}_battery_temperature" for device in devices])
        
        # Battery capacity and limits from cached details
        self.set_arg("soc_max", [f"sensor.predbat_solis_{device}_battery_capacity" for device in devices])
        
        # Reserve and limits
        self.set_arg("reserve", [f"number.predbat_solis_{device}_reserve_soc" for device in devices])
        self.set_arg("battery_min_soc", [f"sensor.predbat_solis_{device}_over_discharge_soc" for device in devices])
        
        # Charge/discharge controls - using slot 1 for Predbat primary control
        self.set_arg("charge_start_time", [f"select.predbat_solis_{device}_charge_slot1_start_time" for device in devices])
        self.set_arg("charge_end_time", [f"select.predbat_solis_{device}_charge_slot1_end_time" for device in devices])  # Same selector, parsed
        self.set_arg("charge_limit", [f"number.predbat_solis_{device}_charge_slot1_soc" for device in devices])
        self.set_arg("charge_rate", [f"number.predbat_solis_{device}_charge_slot1_power" for device in devices])
        self.set_arg("scheduled_charge_enable", [f"switch.predbat_solis_{device}_charge_slot1_enable" for device in devices])
        
        self.set_arg("discharge_start_time", [f"select.predbat_solis_{device}_discharge_slot1_start_time" for device in devices])
        self.set_arg("discharge_end_time", [f"select.predbat_solis_{device}_discharge_slot1_end_time" for device in devices])
        self.set_arg("discharge_target_soc", [f"number.predbat_solis_{device}_discharge_slot1_soc" for device in devices])
        self.set_arg("discharge_rate", [f"number.predbat_solis_{device}_discharge_slot1_power" for device in devices])
        self.set_arg("scheduled_discharge_enable", [f"switch.predbat_solis_{device}_discharge_slot1_enable" for device in devices])
        self.set_arg("battery_rate_max", [f"number.predbat_solis_{device}_max_charge_power" for device in devices])
        
        self.log("Solis API: Automatic configuration complete")
    
    async def poll_inverter_data(self, inverter_sn, cid_list, batch=True):
        """Poll CID values for specific inverter"""
        self.log("Solis API: Polling data for inverter {}".format(inverter_sn))
        try:
            if batch:
                values = await self.read_batch(inverter_sn, cid_list)
            else:
                values = {}
                for cid in cid_list:
                    value = await self.read_cid(inverter_sn, cid)
                    values[cid] = value
            
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
        
        self.log(f"Solis API: Calculated max currents for {inverter_sn}: charge={max_charge}A, discharge={max_discharge}A")
    
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
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_today_export_energy"
            total_export = detail.get("gridSellTodayEnergy")
            total_export_units = detail.get("gridSellTodayEnergyStr", "kWh")
            self.dashboard_item(
                entity_id,
                state=total_export,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Today Export Energy",
                    "unit_of_measurement": total_export_units,
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:transmission-tower-export",
                },
                app="solis"
            )
            
            # Total Import Energy
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_today_import_energy"
            total_import = detail.get("gridPurchasedTodayEnergy")
            total_import_units = detail.get("gridPurchasedTodayEnergyStr", "kWh")
            self.dashboard_item(
                entity_id,
                state=total_import,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Today Import Energy",
                    "unit_of_measurement": total_import_units,
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:transmission-tower-import",
                },
                app="solis"
            )
            
            # Battery Capacity SOC (from detail API, not live CID)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_battery_soc"
            battery_soc = detail.get("batteryCapacitySoc")
            self.dashboard_item(
                entity_id,
                state=battery_soc,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery SOC",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "icon": "mdi:battery-50",
                },
                app="solis"
            )
            
            # detail contains: maxChargePowerW which can be used for battery_rate_max
            max_charge_power_detail = detail.get("maxChargePowerW")
            print("Max Charge Power from detail:", max_charge_power_detail)

            # PV Energy Total
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_pv_energy_total"
            pv_total = detail.get("eTotal")
            pv_total_unit = detail.get("eTotalStr", "kWh")
            if pv_total_unit == 'MWh':
                try:
                    pv_total = float(pv_total) * 1000.0
                    pv_total_unit = 'kWh'
                except (ValueError, TypeError):
                    pass
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
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_pv_power"
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
            
            # Product Model
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_product_model"
            product_model = detail.get("productModel")
            self.dashboard_item(
                entity_id,
                state=product_model,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Product Model",
                    "icon": "mdi:information-outline",
                },
                app="solis"
            )
            
            # Inverter Temperature
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_inverter_temperature"
            inverter_temp = detail.get("inverterTemperature")
            self.dashboard_item(
                entity_id,
                state=inverter_temp,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Inverter Temperature",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                    "icon": "mdi:thermometer",
                },
                app="solis"
            )
            
            # Battery Power (from detail)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_battery_power"
            battery_power = detail.get("batteryPower")
            battery_power_unit = detail.get("batteryPowerStr", "kW")
            self.dashboard_item(
                entity_id,
                state=battery_power,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Power",
                    "unit_of_measurement": battery_power_unit,
                    "device_class": "power",
                    "state_class": "measurement",
                    "icon": "mdi:battery-charging",
                },
                app="solis"
            )
            
            # Battery Voltage (from detail)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_battery_voltage"
            battery_voltage = detail.get("batteryVoltage")
            battery_voltage_unit = detail.get("batteryVoltageStr", "V")
            self.dashboard_item(
                entity_id,
                state=battery_voltage,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Voltage",
                    "unit_of_measurement": battery_voltage_unit,
                    "device_class": "voltage",
                    "state_class": "measurement",
                    "icon": "mdi:lightning-bolt",
                },
                app="solis"
            )
            
            # Battery Current (from detail)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_battery_current"
            battery_current = detail.get("batteryCurrent")
            battery_current_unit = detail.get("batteryCurrentStr", "A")
            self.dashboard_item(
                entity_id,
                state=battery_current,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Battery Current",
                    "unit_of_measurement": battery_current_unit,
                    "device_class": "current",
                    "state_class": "measurement",
                    "icon": "mdi:current-dc",
                },
                app="solis"
            )
            
            # Load Power (from detail)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_load_power"
            load_power = detail.get("familyLoadPower")
            load_power_unit = detail.get("familyLoadPowerStr", "kW")
            self.dashboard_item(
                entity_id,
                state=load_power,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Load Power",
                    "unit_of_measurement": load_power_unit,
                    "device_class": "power",
                    "state_class": "measurement",
                    "icon": "mdi:home-lightning-bolt",
                },
                app="solis"
            )
            
            # Grid Power (from detail)
            entity_id = f"sensor.{prefix}_solis_{inverter_sn}_grid_power"
            grid_power = detail.get("psum")
            grid_power_unit = detail.get("psumStr", "kW")
            self.dashboard_item(
                entity_id,
                state=grid_power,
                attributes={
                    "friendly_name": f"Solis {inverter_name} Grid Power",
                    "unit_of_measurement": grid_power_unit,
                    "device_class": "power",
                    "state_class": "measurement",
                    "icon": "mdi:transmission-tower",
                },
                app="solis"
            )
            
            # Get decoded time windows if available (works with both old and new methods)
            time_windows = self.charge_discharge_time_windows.get(inverter_sn, {})
            
            # Publish charge slot controls (only for slots present in decoded data)
            for slot_num in range(1, 7):
                # Get slot data from decoded windows - skip if not available
                slot_data = time_windows.get(slot_num, None)
                if slot_data is None:
                    continue
                
                # Enable switch
                if "charge_enable" in slot_data:
                    entity_id = f"switch.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_enable"
                    state = str(slot_data["charge_enable"])
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
                if "charge_start_time" in slot_data:
                    entity_id = f"select.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_start_time"
                    start_time = slot_data["charge_start_time"]
                    # Convert HH:MM to HH:MM:00 format
                    if start_time and ":" in start_time and len(start_time.split(":")) == 2:
                        time_value = start_time + ":00"
                    else:
                        time_value = start_time or "00:00:00"
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
                
                # End time selector
                if "charge_end_time" in slot_data:
                    entity_id = f"select.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_end_time"
                    end_time = slot_data["charge_end_time"]
                    # Convert HH:MM to HH:MM:00 format
                    if end_time and ":" in end_time and len(end_time.split(":")) == 2:
                        end_time_value = end_time + ":00"
                    else:
                        end_time_value = end_time or "00:00:00"
                    self.dashboard_item(
                        entity_id,
                        state=end_time_value,
                        attributes={
                            "friendly_name": f"Solis {inverter_name} Charge Slot {slot_num} End Time",
                            "options": OPTIONS_TIME,
                            "icon": "mdi:clock-end",
                        },
                        app="solis"
                    )
                
                # SOC target number
                if "charge_soc" in slot_data:
                    entity_id = f"number.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_soc"
                    soc_value = slot_data["charge_soc"]
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
                if "charge_current" in slot_data:
                    entity_id = f"number.{prefix}_solis_{inverter_sn}_charge_slot{slot_num}_power"
                    current_value_amps = slot_data["charge_current"]
                    
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
            
            # Publish discharge slot controls (only for slots present in decoded data)
            for slot_num in range(1, 7):
                # Get slot data from decoded windows - skip if not available
                slot_data = time_windows.get(slot_num, None)
                if slot_data is None:
                    continue
                
                # Enable switch
                if "discharge_enable" in slot_data:
                    entity_id = f"switch.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_enable"
                    state = str(slot_data["discharge_enable"])
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
                if "discharge_start_time" in slot_data:
                    entity_id = f"select.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_start_time"
                    start_time = slot_data["discharge_start_time"]
                    # Convert HH:MM to HH:MM:00 format
                    if start_time and ":" in start_time and len(start_time.split(":")) == 2:
                        time_value = start_time + ":00"
                    else:
                        time_value = start_time or "00:00:00"
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
                if "discharge_end_time" in slot_data:
                    entity_id = f"select.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_end_time"
                    end_time = slot_data["discharge_end_time"]
                    # Convert HH:MM to HH:MM:00 format
                    if end_time and ":" in end_time and len(end_time.split(":")) == 2:
                        end_time_value = end_time + ":00"
                    else:
                        end_time_value = end_time or "00:00:00"
                    self.dashboard_item(
                        entity_id,
                        state=end_time_value,
                        attributes={
                            "friendly_name": f"Solis {inverter_name} Discharge Slot {slot_num} End Time",
                            "options": OPTIONS_TIME,
                            "icon": "mdi:clock-end",
                        },
                        app="solis"
                    )
                
                # SOC target number
                if "discharge_soc" in slot_data:
                    entity_id = f"number.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_soc"
                    soc_value = slot_data["discharge_soc"]
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
                if "discharge_current" in slot_data:
                    entity_id = f"number.{prefix}_solis_{inverter_sn}_discharge_slot{slot_num}_power"
                    current_value_amps = slot_data["discharge_current"]
                    
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

            battery_capacity_ah = values.get(SOLIS_CID_BATTERY_CAPACITY, None)
            if battery_capacity_ah is not None:
                try:
                    battery_capacity_ah = float(battery_capacity_ah)
                    battery_capacity_kWh = battery_capacity_ah * self.nominal_voltage / 1000.0
                    entity_id = f"sensor.{prefix}_solis_{inverter_sn}_battery_capacity"
                    self.dashboard_item(
                        entity_id,
                        state=round(battery_capacity_kWh, 2),
                        attributes={
                            "friendly_name": f"Solis {inverter_name} Battery Capacity",
                            "unit_of_measurement": "kWh",
                            "device_class": "energy",
                            "state_class": "measurement",
                            "icon": "mdi:battery",
                        },
                        app="solis"
                    )
                except (ValueError, TypeError):
                    pass
    
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
                success = await self.read_and_write_cid(inverter_sn, enable_cid, enable, field_description=f"charge slot {slot_num} enable")  # Enable
                
                time_cid = SOLIS_CID_CHARGE_TIME[slot_num - 1]
                success &= await self.read_and_write_cid(inverter_sn, time_cid, time_str, field_description=f"charge slot {slot_num} time")  # Time
                
                soc_cid = SOLIS_CID_CHARGE_SOC_BASE + (slot_num - 1)
                success &= await self.read_and_write_cid(inverter_sn, soc_cid, soc, field_description=f"charge slot {slot_num} SOC")  # SOC
                
                current_cid = SOLIS_CID_CHARGE_CURRENT[slot_num - 1]
                success &= await self.read_and_write_cid(inverter_sn, current_cid, current, field_description=f"charge slot {slot_num} current")  # Current
                
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
                success = await self.read_and_write_cid(inverter_sn, enable_cid, enable, field_description=f"discharge slot {slot_num} enable")  # Enable
                
                time_cid = SOLIS_CID_DISCHARGE_TIME[slot_num - 1]
                success &= await self.read_and_write_cid(inverter_sn, time_cid, time_str, field_description=f"discharge slot {slot_num} time")  # Time
                
                soc_cid = SOLIS_CID_DISCHARGE_SOC[slot_num - 1]
                success &= await self.read_and_write_cid(inverter_sn, soc_cid, soc, field_description=f"discharge slot {slot_num} SOC")  # SOC
                
                current_cid = SOLIS_CID_DISCHARGE_CURRENT[slot_num - 1]
                success &= await self.read_and_write_cid(inverter_sn, current_cid, current, field_description=f"discharge slot {slot_num} current")  # Current                

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
            success = await self.read_and_write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, mode_value, field_description=f"storage mode to {mode}")
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
                
                # Strip :SS from HH:MM:SS
                new_hhmm = value[:5] if len(value) >= 5 else value
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                if "start_time" in field:
                    self.charge_discharge_time_windows[inverter_sn][slot_num]["charge_start_time"] = new_hhmm
                elif "end_time" in field:
                    self.charge_discharge_time_windows[inverter_sn][slot_num]["charge_end_time"] = new_hhmm
                
                # Write will happen in the main loop
                
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
                
                # Strip :SS from HH:MM:SS
                new_hhmm = value[:5] if len(value) >= 5 else value
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                if "start_time" in field:
                    self.charge_discharge_time_windows[inverter_sn][slot_num]["discharge_start_time"] = new_hhmm
                elif "end_time" in field:
                    self.charge_discharge_time_windows[inverter_sn][slot_num]["discharge_end_time"] = new_hhmm
                
                # Write will happen in the main loop
                
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
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                self.charge_discharge_time_windows[inverter_sn][slot_num]["charge_soc"] = float(value_str)
                
                # Write will happen in the main loop
                
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
                
                # Convert watts to amps for inverter
                amps = int(value / self.nominal_voltage)
                amps_str = str(amps)
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                self.charge_discharge_time_windows[inverter_sn][slot_num]["charge_current"] = float(amps)
                
                # Write will happen in the main loop
                
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
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                self.charge_discharge_time_windows[inverter_sn][slot_num]["discharge_soc"] = float(value_str)
                
                # Write will happen in the main loop
                
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
                
                # Convert watts to amps for inverter
                amps = int(value / self.nominal_voltage)
                amps_str = str(amps)
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                self.charge_discharge_time_windows[inverter_sn][slot_num]["discharge_current"] = float(amps)
                
                # Write will happen in the main loop
                
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
                await self.read_and_write_cid(inverter_sn, cid, value_str, field_description=f"{field} to {value_str}%")
                
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
                await self.read_and_write_cid(inverter_sn, cid, amps_str, field_description=f"{field} to {value_str}W ({amps}A)")
                
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
                await self.read_and_write_cid(inverter_sn, cid, value_str, field_description=f"{field} to {value_str}")
                
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
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                self.charge_discharge_time_windows[inverter_sn][slot_num]["charge_enable"] = int(value)
                
                # Write will happen in the main loop
                
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
                
                # Update charge_discharge_time_windows cache
                if inverter_sn not in self.charge_discharge_time_windows:
                    self.charge_discharge_time_windows[inverter_sn] = {}
                if slot_num not in self.charge_discharge_time_windows[inverter_sn]:
                    self.charge_discharge_time_windows[inverter_sn][slot_num] = {}
                
                self.charge_discharge_time_windows[inverter_sn][slot_num]["discharge_enable"] = int(value)

                # Write will happen in the main loop not here
                
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
                await self.read_and_write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, str(new_mode), field_description=f"battery reserve to {service} (mode: {current_mode} -> {new_mode})")
                
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
                await self.read_and_write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, str(new_mode), field_description=f"allow grid charging to {service} (mode: {current_mode} -> {new_mode})")
                
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
                await self.read_and_write_cid(inverter_sn, SOLIS_CID_STORAGE_MODE, str(new_mode), field_description=f"time of use to {service} (mode: {current_mode} -> {new_mode})")
                
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
                await self.read_and_write_cid(inverter_sn, SOLIS_CID_ALLOW_EXPORT, new_value, field_description=f"allow export to {service} (value: {current_value} -> {new_value})")
                
                # Re-publish entities
                await self.publish_entities()
                return
        
        except Exception as e:
            self.log(f"Error: Solis API switch_event failed for {entity_id}: {e}")

    async def fetch_inverter_details(self, sn):
        try:
            detail = await self.get_inverter_detail(sn)
            self.inverter_details[sn] = detail
            self.log(f"Solis API: Loaded details for inverter {sn} - {detail}")
            
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

    def is_tou_v2_mode(self, sn):
        """
        Check if Time of Use V2 mode is enabled for the given inverter serial number.
        Returns "1" if enabled, "0" otherwise.
        """
        result = self.cached_values.get(sn, {}).get(SOLIS_CID_TOU_V2_MODE, "0")
        try:
            result = int(result)
        except (ValueError, TypeError):
            result = 0
        if result == 43605:
            return True
        else:
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
                await self.poll_inverter_data(sn, [SOLIS_CID_TOU_V2_MODE])  # Get TOU V2 mode status
                if self.is_tou_v2_mode(sn):
                    self.log(f"Solis API: Inverter {sn} is in Time of Use V2 mode")
                else:
                    self.log(f"Solis API: Inverter {sn} is in standard Time of Use mode")

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
                success =  await self.fetch_inverter_details(sn) # Get inverter details for all inverters
                if not success:
                    poll_success = False                
            
            # Only update last_updated_time if all polls succeeded
            if poll_success:
                self.last_updated_time = self.base.now_utc
        
        # Infrequent polling (every 60 minutes)
        if first or (seconds % 3600 == 0):
            for sn in self.inverter_sn:
                self.log(f"Solis API: Performing infrequent data poll for inverter {sn}...")
                await self.poll_inverter_data(sn, SOLIS_CID_INFREQUENT)
                if self.is_tou_v2_mode(sn):
                    success = await self.poll_inverter_data(sn, SOLIS_CID_LIST_TOU_V2)
                    if not success:
                        poll_success = False
                    await self.decode_time_windows_v2(sn)
                else:
                    self.log("Solis API: Inverter is in standard Time of Use mode, polling standard TOU data")
                    success =  await self.poll_inverter_data(sn, SOLIS_CID_SINGLE, batch=False)
                    if not success:
                        poll_success = False
                    await self.decode_time_windows(sn)
                # Recalculate max currents after polling infrequent data
                self._calculate_max_currents(sn)

        # Control mode
        if first or (seconds % 60 == 0):
            # Write to inverter using new function (handles both V1 and V2)
            is_readonly = self.get_state_wrapper(f'switch.{self.prefix}_set_read_only', default='off') == 'on'
            if self.control_enable and not is_readonly:
                for sn in self.inverter_sn:
                    await self.reset_charge_windows_if_needed(sn)
                    await self.write_time_windows_if_changed(sn)
            else:
                self.log("Solis API: Control disabled, skipping writing time windows")

        
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
    arg_dict = {"api_key": key_id, "api_secret": secret, "automatic": True, "control_enable": True}
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
